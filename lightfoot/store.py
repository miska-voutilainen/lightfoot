"""SQLite store: launches, sessions, frozen rewrites, the cost ledger, signals and events.

One connection guarded by a lock; every call is short. WAL mode lets `lightfoot stats`
read while the proxy writes.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .config import lightfoot_home, private_dir

SCHEMA = """
CREATE TABLE IF NOT EXISTS launches (
    id TEXT PRIMARY KEY,
    cwd TEXT,
    mode TEXT,
    upstream TEXT,
    graphify_report TEXT,
    vault TEXT,
    label TEXT,
    created REAL
);
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    launch_id TEXT,
    mode TEXT,
    first_seen REAL,
    last_request REAL,
    turns INTEGER DEFAULT 0,
    model TEXT,
    context_tokens INTEGER DEFAULT 0,
    epochs INTEGER DEFAULT 0,
    last_epoch_turn INTEGER DEFAULT 0,
    epochs_disabled INTEGER DEFAULT 0,
    pending_write INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS rewrites (
    tool_use_id TEXT NOT NULL,
    live INTEGER NOT NULL,
    orig_hash TEXT NOT NULL,
    text TEXT NOT NULL,
    rule TEXT NOT NULL,
    epoch_turn INTEGER,
    session_id TEXT,
    path TEXT,
    created REAL,
    PRIMARY KEY (tool_use_id, live)
);
CREATE INDEX IF NOT EXISTS rewrites_path ON rewrites(path);
CREATE TABLE IF NOT EXISTS ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL,
    launch_id TEXT,
    session_id TEXT,
    mode TEXT,
    model TEXT,
    status INTEGER,
    turn INTEGER,
    input INTEGER DEFAULT 0,
    cw5 INTEGER DEFAULT 0,
    cw1h INTEGER DEFAULT 0,
    cr INTEGER DEFAULT 0,
    output INTEGER DEFAULT 0,
    orig_tokens INTEGER DEFAULT 0,
    saved_tokens INTEGER DEFAULT 0,
    rules TEXT,
    epoch INTEGER DEFAULT 0,
    attempt TEXT,
    latency_ms REAL
);
CREATE INDEX IF NOT EXISTS ledger_session ON ledger(session_id);
CREATE INDEX IF NOT EXISTS ledger_launch ON ledger(launch_id);
CREATE TABLE IF NOT EXISTS signals (
    tool_use_id TEXT PRIMARY KEY,
    ts REAL,
    session_id TEXT,
    rule TEXT,
    kind TEXT,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL,
    level TEXT,
    session_id TEXT,
    kind TEXT,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS prunes (
    session_id TEXT NOT NULL,
    live INTEGER NOT NULL,
    boundary INTEGER NOT NULL,
    fingerprint TEXT NOT NULL,
    summary TEXT NOT NULL,
    history_path TEXT,
    dropped_tokens INTEGER DEFAULT 0,
    created REAL,
    kind TEXT DEFAULT 'extractive',
    pending INTEGER DEFAULT 0,
    PRIMARY KEY (session_id, live)
);
CREATE TABLE IF NOT EXISTS hook_rewrites (
    ts REAL,
    session_id TEXT,
    cwd TEXT,
    tool TEXT,
    rule TEXT,
    orig_tokens INTEGER,
    new_tokens INTEGER
);
CREATE TABLE IF NOT EXISTS limits (
    ts REAL,
    launch_id TEXT,
    window TEXT,
    used_pct REAL,
    resets_at INTEGER
);
CREATE INDEX IF NOT EXISTS limits_launch ON limits(launch_id, window, ts);
CREATE TABLE IF NOT EXISTS prefixes (
    session_id TEXT PRIMARY KEY,
    launch_id TEXT,
    ts REAL,
    model TEXT,
    blocks TEXT,
    tools TEXT
);
CREATE TABLE IF NOT EXISTS tools_used (
    session_id TEXT,
    name TEXT,
    PRIMARY KEY (session_id, name)
);
"""

# Migrations run in order on open; each adds what a newer version needs. Column
# additions are applied only if the column is missing, so re-running is harmless.
SCHEMA_VERSION = 5
MIGRATIONS: dict[int, list[tuple[str, str, str]]] = {
    2: [
        ("ledger", "agent", "TEXT"),
        ("ledger", "client_version", "TEXT"),
        ("launches", "agent", "TEXT"),
        ("launches", "experimental", "INTEGER DEFAULT 0"),
    ],
    3: [
        ("sessions", "floor_tokens", "INTEGER DEFAULT 0"),
    ],
    4: [
        ("prunes", "kind", "TEXT DEFAULT 'extractive'"),
        ("prunes", "pending", "INTEGER DEFAULT 0"),
    ],
    5: [
        ("ledger", "side", "INTEGER DEFAULT 0"),
    ],
}


@dataclass
class Launch:
    """One `lightfoot <agent>` run (or a lasting extension setup): its project, mode, upstream and integrations.

    Requests name their launch in the URL path (`/l/<id>`), which is how the proxy knows the project.
    """

    id: str
    cwd: str | None
    mode: str | None
    upstream: str | None
    graphify_report: str | None
    vault: str | None
    label: str | None = None
    agent: str = "claude"
    experimental: bool = False


@dataclass
class Session:
    """Per-conversation state: mode, last request time, context size and the floor pruning measures from."""

    id: str
    launch_id: str | None
    mode: str
    first_seen: float
    last_request: float | None
    turns: int
    epochs: int
    last_epoch_turn: int
    epochs_disabled: bool
    pending_write: int
    context_tokens: int = 0  # prompt size of the session's last response (real tokens)
    floor_tokens: int = 0  # prompt size of its first response: system prompt, tools, first message


@dataclass
class Prune:
    """A frozen old-turn prune: messages[1:boundary] replaced by `summary`.

    kind "compaction": messages[:boundary] replaced by a signed compaction block from the API
    (`summary` holds its JSON). `pending` until the next user prompt, so it never lands mid-task.
    """

    session_id: str
    live: bool
    boundary: int
    fingerprint: str  # of messages[:boundary], to confirm the same history is being re-sent
    summary: str
    history_path: str | None
    dropped_tokens: int
    kind: str = "extractive"
    pending: bool = False


@dataclass
class Rewrite:
    """A frozen tool-output rewrite, keyed by tool call id. Shadow and live rewrites are stored apart."""

    tool_use_id: str
    live: bool
    orig_hash: str
    text: str
    rule: str
    epoch_turn: int | None = None
    session_id: str | None = None
    path: str | None = None


def default_db_path() -> Path:
    """`~/.lightfoot/lightfoot.db`, or the same file under `LIGHTFOOT_HOME`."""
    return lightfoot_home() / "lightfoot.db"


class Store:
    """The SQLite database behind everything lightfoot remembers.

    One connection in WAL mode, shared across threads behind a lock. Every call is short,
    so one connection is enough, and WAL lets `lightfoot stats` read while the proxy writes.
    """

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else default_db_path()
        private_dir(self.path.parent)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False, timeout=10)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
            self._db.executescript(SCHEMA)
            self._db.commit()
            self._migrate()

    def _migrate(self) -> None:
        row = self._db.execute("SELECT value FROM state WHERE key='schema_version'").fetchone()
        version = int(row["value"]) if row else 1
        if version > SCHEMA_VERSION:
            raise RuntimeError(f"{self.path} was written by a newer lightfoot (schema {version}); upgrade lightfoot")
        for target in range(version + 1, SCHEMA_VERSION + 1):
            for table, column, decl in MIGRATIONS.get(target, []):
                existing = {r["name"] for r in self._db.execute(f"PRAGMA table_info({table})")}
                if column not in existing:
                    self._db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        self._db.execute(
            "INSERT INTO state(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )
        self._db.commit()

    def close(self) -> None:
        """Close the database connection."""
        with self._lock:
            self._db.close()

    def _exec(self, sql: str, params: tuple | list = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._db.execute(sql, params)
            self._db.commit()
            return cur

    def execute(self, sql: str, params: tuple | list = ()) -> sqlite3.Cursor:
        """Run one write statement and commit."""
        return self._exec(sql, params)

    def query(self, sql: str, params: tuple | list = ()) -> list[sqlite3.Row]:
        """Run a read query and return all rows."""
        with self._lock:
            return self._db.execute(sql, params).fetchall()

    # --- state -----------------------------------------------------------------
    def get_state(self, key: str, default: str | None = None) -> str | None:
        """A value from the key-value `state` table, or `default`."""
        rows = self.query("SELECT value FROM state WHERE key=?", (key,))
        return rows[0]["value"] if rows else default

    def set_state(self, key: str, value: str | None) -> None:
        """Set a `state` value; None deletes the key."""
        if value is None:
            self._exec("DELETE FROM state WHERE key=?", (key,))
        else:
            self._exec(
                "INSERT INTO state(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    # --- launches --------------------------------------------------------------
    def put_launch(self, launch: Launch) -> None:
        """Insert or replace a launch."""
        self._exec(
            "INSERT OR REPLACE INTO launches(id, cwd, mode, upstream, graphify_report, vault, label, created, "
            "agent, experimental) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                launch.id,
                launch.cwd,
                launch.mode,
                launch.upstream,
                launch.graphify_report,
                launch.vault,
                launch.label,
                time.time(),
                launch.agent,
                int(launch.experimental),
            ),
        )

    def get_launch(self, launch_id: str) -> Launch | None:
        """The launch with this id, or None."""
        rows = self.query("SELECT * FROM launches WHERE id=?", (launch_id,))
        if not rows:
            return None
        r = rows[0]
        return Launch(
            r["id"],
            r["cwd"],
            r["mode"],
            r["upstream"],
            r["graphify_report"],
            r["vault"],
            r["label"],
            r["agent"] or "claude",
            bool(r["experimental"]),
        )

    # --- sessions --------------------------------------------------------------
    def get_or_create_session(self, session_id: str, launch_id: str | None, mode: str, now: float) -> Session:
        """The session with this id, created on first sight with the given launch and mode."""
        with self._lock:
            self._db.execute(
                "INSERT OR IGNORE INTO sessions(id, launch_id, mode, first_seen, last_request) "
                "VALUES(?, ?, ?, ?, NULL)",
                (session_id, launch_id, mode, now),
            )
            self._db.commit()
            r = self._db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        return Session(
            r["id"],
            r["launch_id"],
            r["mode"],
            r["first_seen"],
            r["last_request"],
            r["turns"] or 0,
            r["epochs"] or 0,
            r["last_epoch_turn"] or 0,
            bool(r["epochs_disabled"]),
            r["pending_write"] or 0,
            r["context_tokens"] or 0,
            r["floor_tokens"] or 0,
        )

    def touch_session(self, session_id: str, now: float, turns: int, model: str | None) -> None:
        """Record a request. The context size is left alone: it comes from the response (set_context)."""
        self._exec("UPDATE sessions SET last_request=?, turns=?, model=? WHERE id=?", (now, turns, model, session_id))

    def set_session_mode(self, session_id: str, mode: str) -> None:
        """Change a session's mode, for example to passthrough after its stored rewrites were rejected."""
        self._exec("UPDATE sessions SET mode=? WHERE id=?", (mode, session_id))

    def set_context(self, session_id: str, context_tokens: int) -> None:
        """Prompt size of the latest response; the first one also becomes the session's floor."""
        self._exec(
            "UPDATE sessions SET context_tokens=?, floor_tokens=CASE WHEN COALESCE(floor_tokens, 0)=0 "
            "THEN ? ELSE floor_tokens END WHERE id=?",
            (context_tokens, context_tokens, session_id),
        )

    # --- prunes ----------------------------------------------------------------
    def get_prune(self, session_id: str, live: bool) -> Prune | None:
        """The session's frozen prune (shadow or live), or None."""
        rows = self.query("SELECT * FROM prunes WHERE session_id=? AND live=?", (session_id, int(live)))
        if not rows:
            return None
        r = rows[0]
        return Prune(
            r["session_id"],
            bool(r["live"]),
            r["boundary"],
            r["fingerprint"],
            r["summary"],
            r["history_path"],
            r["dropped_tokens"] or 0,
            r["kind"] or "extractive",
            bool(r["pending"]),
        )

    def put_prune(self, prune: Prune) -> None:
        """Insert or replace the session's prune."""
        self._exec(
            "INSERT OR REPLACE INTO prunes(session_id, live, boundary, fingerprint, summary, history_path, "
            "dropped_tokens, created, kind, pending) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                prune.session_id,
                int(prune.live),
                prune.boundary,
                prune.fingerprint,
                prune.summary,
                prune.history_path,
                prune.dropped_tokens,
                time.time(),
                prune.kind,
                int(prune.pending),
            ),
        )

    def activate_prune(self, session_id: str, live: bool) -> None:
        """A pending (background) prune takes effect: from now on it is replayed on every request."""
        self._exec("UPDATE prunes SET pending=0 WHERE session_id=? AND live=?", (session_id, int(live)))

    def restore_prune(self, added: Prune, replaced: Prune | None) -> None:
        """Undo a put_prune."""
        self._exec("DELETE FROM prunes WHERE session_id=? AND live=?", (added.session_id, int(added.live)))
        if replaced is not None:
            self.put_prune(replaced)

    def record_epoch(self, session_id: str, turn: int, pending_write: int) -> None:
        """Count an epoch and remember the cache write it predicted, to check against the next response."""
        self._exec(
            "UPDATE sessions SET epochs=epochs+1, last_epoch_turn=?, pending_write=? WHERE id=?",
            (turn, pending_write, session_id),
        )

    def clear_pending_write(self, session_id: str) -> None:
        """The predicted cache write matched the response: nothing left to check."""
        self._exec("UPDATE sessions SET pending_write=0 WHERE id=?", (session_id,))

    def disable_epochs(self, session_id: str) -> None:
        """Stop epochs for a session whose cache write came out far above the prediction."""
        self._exec("UPDATE sessions SET epochs_disabled=1, pending_write=0 WHERE id=?", (session_id,))

    # --- rewrites --------------------------------------------------------------
    def get_rewrites(self, tool_use_ids: list[str], live: bool) -> dict[str, Rewrite]:
        """Stored rewrites for these tool call ids, by id. Queried in chunks to stay under SQLite's variable limit."""
        found: dict[str, Rewrite] = {}
        ids = list(dict.fromkeys(tool_use_ids))
        for start in range(0, len(ids), 500):
            chunk = ids[start : start + 500]
            marks = ",".join("?" * len(chunk))
            # Only `?` placeholders are spliced in; values are bound.
            query = f"SELECT * FROM rewrites WHERE live=? AND tool_use_id IN ({marks})"  # noqa: S608  # nosec B608
            for r in self.query(query, [int(live), *chunk]):
                found[r["tool_use_id"]] = Rewrite(
                    r["tool_use_id"],
                    bool(r["live"]),
                    r["orig_hash"],
                    r["text"],
                    r["rule"],
                    r["epoch_turn"],
                    r["session_id"],
                    r["path"],
                )
        return found

    def put_rewrites(self, rewrites: list[Rewrite]) -> None:
        """Insert or replace rewrites in one transaction."""
        if not rewrites:
            return
        now = time.time()
        with self._lock:
            self._db.executemany(
                "INSERT OR REPLACE INTO rewrites(tool_use_id, live, orig_hash, text, rule, epoch_turn, session_id, "
                "path, created) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (r.tool_use_id, int(r.live), r.orig_hash, r.text, r.rule, r.epoch_turn, r.session_id, r.path, now)
                    for r in rewrites
                ],
            )
            self._db.commit()

    def restore_rewrites(self, added: list[Rewrite], replaced: list[Rewrite]) -> None:
        """Undo a put_rewrites: delete what was added, put back what it overwrote."""
        self.delete_rewrites([(r.tool_use_id, int(r.live)) for r in added])
        self.put_rewrites(replaced)

    def delete_rewrites(self, keys: list[tuple[str, int]]) -> None:
        """Delete rewrites by (tool_use_id, live)."""
        with self._lock:
            for start in range(0, len(keys), 500):
                self._db.executemany("DELETE FROM rewrites WHERE tool_use_id=? AND live=?", keys[start : start + 500])
            self._db.commit()

    def rewrites_by_path(self, path: str, live: bool) -> list[Rewrite]:
        """Every stored rewrite whose full text was saved at `path` (for `lightfoot why` and cleanup)."""
        rows = self.query("SELECT * FROM rewrites WHERE live=? AND path=?", (int(live), path))
        return [
            Rewrite(
                r["tool_use_id"],
                bool(r["live"]),
                r["orig_hash"],
                r["text"],
                r["rule"],
                r["epoch_turn"],
                r["session_id"],
                r["path"],
            )
            for r in rows
        ]

    # --- ledger, signals, events -------------------------------------------------
    def add_ledger(self, row: dict) -> None:
        """Write one ledger row; `rules` may be a dict and is stored as JSON."""
        cols = [
            "ts",
            "launch_id",
            "session_id",
            "mode",
            "model",
            "status",
            "turn",
            "input",
            "cw5",
            "cw1h",
            "cr",
            "output",
            "orig_tokens",
            "saved_tokens",
            "rules",
            "epoch",
            "attempt",
            "latency_ms",
            "agent",
            "client_version",
            "side",
        ]
        values = [row.get(c) for c in cols]
        rules_idx = cols.index("rules")
        if isinstance(values[rules_idx], dict):
            values[rules_idx] = json.dumps(values[rules_idx], sort_keys=True)
        # The column names are the fixed list above; values are bound.
        sql = f"INSERT INTO ledger({', '.join(cols)}) VALUES({', '.join('?' * len(cols))})"  # nosec B608
        self._exec(sql, values)

    def put_prefix(self, session_id: str, launch_id: str | None, model: str | None, blocks: list, tools: dict) -> None:
        """A session's fixed prefix, recorded once: per system block and tool, a hash, tokens and a short label."""
        self._exec(
            "INSERT OR IGNORE INTO prefixes(session_id, launch_id, ts, model, blocks, tools) VALUES(?, ?, ?, ?, ?, ?)",
            (session_id, launch_id, time.time(), model, json.dumps(blocks), json.dumps(tools)),
        )

    def add_tools_used(self, session_id: str, names: list[str]) -> None:
        """Record tool names a session called, once each (for `lightfoot audit`)."""
        with self._lock:
            self._db.executemany(
                "INSERT OR IGNORE INTO tools_used(session_id, name) VALUES(?, ?)", [(session_id, n) for n in names]
            )
            self._db.commit()

    def add_signal(self, tool_use_id: str, session_id: str, rule: str, kind: str, detail: str) -> None:
        """Record an over-compression signal, once per tool call."""
        self._exec(
            "INSERT OR IGNORE INTO signals(tool_use_id, ts, session_id, rule, kind, detail) VALUES(?, ?, ?, ?, ?, ?)",
            (tool_use_id, time.time(), session_id, rule, kind, detail),
        )

    def event(self, level: str, kind: str, detail: str, session_id: str | None = None) -> None:
        """Log a warning or note for `lightfoot events`; long details are cut at 4,000 characters."""
        self._exec(
            "INSERT INTO events(ts, level, session_id, kind, detail) VALUES(?, ?, ?, ?, ?)",
            (time.time(), level, session_id, kind, detail[:4000]),
        )
