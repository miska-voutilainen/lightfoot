"""Client profiles: what each agent's tools are, in the neutral kinds the rules use.

Kinds: read | search | shell | edit | subagent | graph_query | other.
Profiles are researched from each agent's source (commit noted per profile) and kept
deliberately conservative: anything unrecognised is "other", which only the generic
ingest rules touch.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from typing import ClassVar

from .text import line_number_range

GRAPH_TOOL_RE = re.compile(
    r"graphify[\w-]*__(query_graph|get_node|get_neighbors|get_community|shortest_path|god_nodes|graph_stats)$",
    re.I,
)


@dataclass(frozen=True)
class Profile:
    """How one agent names its tools: maps each call to a neutral kind and reads its paths, commands and searches.

    The base profile knows only graph queries; an agent's subclass adds its own tool names.
    """

    name: str

    def kind(self, name: str, inp: dict) -> str:
        """The neutral kind of a tool call: read, search, shell, edit, subagent, graph_query or other."""
        return "graph_query" if GRAPH_TOOL_RE.search(name or "") else "other"

    def path(self, name: str, inp: dict) -> str | None:
        """File path for read and edit calls."""
        return None

    def read_span(self, text: str) -> tuple[int, int] | None:
        """(first, last) line number covered by a read result, if the format shows it."""
        return None

    def search_content_mode(self, name: str, inp: dict) -> bool:
        """True when a search result lists matching lines (path:line:text), not just paths."""
        return False

    def command(self, name: str, inp: dict) -> str:
        """The command line of a shell call, for the prune summary."""
        return str(inp.get("command", ""))

    def search_text(self, name: str, inp: dict) -> str:
        """What a search call looked for, for the prune summary."""
        return str(inp.get("pattern") or inp.get("path") or "")


class ClaudeCode(Profile):
    """Claude Code 2.1.x tool names (also Claude Agent SDK apps using the built-in tools)."""

    KINDS: ClassVar[dict[str, str]] = {
        "Read": "read",
        "NotebookRead": "read",
        "Write": "edit",
        "Edit": "edit",
        "MultiEdit": "edit",
        "NotebookEdit": "edit",
        "Grep": "search",
        "Glob": "search",
        "LS": "search",
        "Task": "subagent",
        "Agent": "subagent",
        "Bash": "shell",
        "PowerShell": "shell",
    }

    def kind(self, name, inp):
        """Claude Code's tool names, falling back to graph queries and other."""
        return self.KINDS.get(name) or super().kind(name, inp)

    def path(self, name, inp):
        """`file_path`, or `notebook_path` for notebook edits."""
        value = inp.get("file_path") or inp.get("notebook_path")
        return value if isinstance(value, str) else None

    def read_span(self, text):
        """Read results carry `cat -n` style line numbers."""
        return line_number_range(text)

    def search_content_mode(self, name, inp):
        """Grep returns matching lines only with `output_mode = "content"`."""
        return name == "Grep" and inp.get("output_mode") == "content"


_READ_COMMANDS = {"cat", "head", "tail", "nl", "bat", "less", "more", "type", "get-content", "gc"}
_SEARCH_COMMANDS = {"rg", "grep", "egrep", "fgrep", "ag", "ack", "find", "fd", "ls", "tree", "dir", "get-childitem"}
_COMPOUND = re.compile(r"[|;&<>`$]")


def _command_tokens(inp: dict) -> list[str] | None:
    cmd = inp.get("cmd", inp.get("command"))
    if isinstance(cmd, list) and all(isinstance(c, str) for c in cmd):
        # ["bash", "-lc", "cat x"] style: look inside the shell string
        if len(cmd) >= 3 and cmd[-2] in ("-lc", "-c") and isinstance(cmd[-1], str):
            cmd = cmd[-1]
        else:
            return cmd
    if not isinstance(cmd, str) or _COMPOUND.search(cmd):
        return None
    try:
        return shlex.split(cmd, posix=True)
    except ValueError:
        return None


class Codex(Profile):
    """OpenAI Codex CLI (openai/codex, researched 2026-10-25: codex-rs/core/src/tools/handlers).

    exec_command / write_stdin run shell commands; apply_patch is a freeform custom tool.
    A single plain read or search command (no pipes or chaining) is classified by its
    program so the read/search rules apply; anything else is a shell call.
    """

    def kind(self, name, inp):
        """Codex runs most work as shell commands, so reads and searches are told apart by the program they run."""
        if name in ("exec_command", "shell", "local_shell"):
            tokens = _command_tokens(inp)
            if tokens:
                program = tokens[0].rsplit("/", 1)[-1].lower()
                if program in _READ_COMMANDS or (program == "sed" and "-n" in tokens):
                    return "read"
                if program in _SEARCH_COMMANDS or tokens[:2] in (["git", "grep"], ["git", "ls-files"]):
                    return "search"
            return "shell"
        if name == "write_stdin":
            return "shell"
        if name == "apply_patch":
            return "edit"
        # Codex 0.164.0 groups these in its `multi_agent_v1` namespace and adds resume_agent and
        # wait_agent; `wait` is the older name.
        if name in ("spawn_agent", "send_input", "wait", "wait_agent", "resume_agent", "close_agent"):
            return "subagent"
        return super().kind(name, inp)

    def path(self, name, inp):
        """The file a read command opens: its last operand that isn't a flag or a sed range."""
        if self.kind(name, inp) != "read":
            return None
        tokens = _command_tokens(inp) or []
        operands = [t for t in tokens[1:] if not t.startswith("-") and not re.fullmatch(r"\d+(,\d+)?p", t)]
        return operands[-1] if operands else None

    def search_content_mode(self, name, inp):
        """A grep-like program that prints matching lines rather than file names."""
        tokens = _command_tokens(inp) or []
        program = tokens[0].rsplit("/", 1)[-1].lower() if tokens else ""
        return (
            program in ("rg", "grep", "egrep", "fgrep", "ag", "ack") and "-l" not in tokens and "--files" not in tokens
        )


_OPENCODE_LINE = re.compile(r"^\s*(\d+): ", re.M)


class OpenCode(Profile):
    """opencode (anomalyco/opencode, researched 2026-10-25 at commit b367fdd16829:
    packages/core/src/tool/plugin/*.ts). It speaks the Anthropic Messages API. Read output
    numbers lines as "<line>: <content>"; grep output groups "  Line N: text" under each
    path, which is not the path:line:text form, so content-mode search rules stay off.

    opencode 1.19.4 (run on 2026-10-26) names its shell tool `bash` and its subagent tool `task`,
    and passes file paths as `filePath`; the names from the researched commit are kept for other versions.
    Its read output still numbers lines as "<line>: <content>", inside <path> and <content> tags.
    """

    KINDS: ClassVar[dict[str, str]] = {
        "read": "read",
        "edit": "edit",
        "write": "edit",
        "patch": "edit",
        "grep": "search",
        "glob": "search",
        "shell": "shell",
        "bash": "shell",
        "subagent": "subagent",
        "task": "subagent",
    }

    def kind(self, name, inp):
        """opencode's tool names, falling back to graph queries and other."""
        return self.KINDS.get(name) or super().kind(name, inp)

    def path(self, name, inp):
        """The `filePath` argument of a read or edit call (`path` in older versions)."""
        if self.KINDS.get(name) not in ("read", "edit"):
            return None
        value = inp.get("filePath") or inp.get("path")
        return value if isinstance(value, str) else None

    def read_span(self, text):
        """Read results number their lines as `N: text`."""
        numbers = _OPENCODE_LINE.findall(text)
        return (int(numbers[0]), int(numbers[-1])) if numbers else None


class GeminiCli(Profile):
    """Gemini CLI (google-gemini/gemini-cli, researched 2026-10-25 at commit cd1b45c2db0a:
    packages/core/src/tools/definitions/base-declarations.ts). read_file returns the file
    without line numbers; grep_search lists "L12: text" lines under "File: path" headers,
    which is not the path:line:text form, so content-mode search rules stay off.
    """

    KINDS: ClassVar[dict[str, str]] = {
        "read_file": "read",
        "read_many_files": "read",
        "write_file": "edit",
        "replace": "edit",
        "grep_search": "search",
        "glob": "search",
        "list_directory": "search",
        "run_shell_command": "shell",
        "invoke_agent": "subagent",
    }

    def kind(self, name, inp):
        """Gemini CLI's tool names, falling back to graph queries and other."""
        return self.KINDS.get(name) or super().kind(name, inp)

    def path(self, name, inp):
        """The `file_path` argument of a read or edit call."""
        value = inp.get("file_path") if self.KINDS.get(name) in ("read", "edit") else None
        return value if isinstance(value, str) else None


class Cline(Profile):
    """Cline (cline/cline, researched 2026-10-25 at commit 40c5d99031fe): the SDK tools
    (sdk/packages/core/src/extensions/tools/constants.ts) that the VS Code extension and CLI
    now run, plus the older names (apps/vscode/src/shared/tools.ts). SDK tools return
    JSON lists of {query, result, success}, so content-mode search rules stay off.
    """

    KINDS: ClassVar[dict[str, str]] = {
        "read_files": "read",
        "search_codebase": "search",
        "run_commands": "shell",
        "editor": "edit",
        "apply_patch": "edit",
        "read_file": "read",
        "write_to_file": "edit",
        "replace_in_file": "edit",
        "search_files": "search",
        "list_files": "search",
        "list_code_definition_names": "search",
        "execute_command": "shell",
        "new_task": "subagent",
        "use_subagents": "subagent",
    }

    def kind(self, name, inp):
        """Cline's tool names, old and new, falling back to graph queries and other."""
        return self.KINDS.get(name) or super().kind(name, inp)

    def path(self, name, inp):
        """The `path` argument, or the single entry of `files` in newer Cline versions."""
        if self.KINDS.get(name) not in ("read", "edit"):
            return None
        value = inp.get("path")
        files = inp.get("files")
        if value is None and isinstance(files, list) and len(files) == 1:
            value = files[0].get("path") if isinstance(files[0], dict) else files[0]
        return value if isinstance(value, str) else None

    def command(self, name, inp):
        """Cline's newer tool takes a list of commands; they are joined as one line."""
        commands = inp.get("commands")
        if isinstance(commands, list):
            return " && ".join(c if isinstance(c, str) else str(c.get("command", "")) for c in commands)
        return super().command(name, inp)

    def search_text(self, name, inp):
        """Cline's newer search takes a list of queries; older versions take one `regex`."""
        queries = inp.get("queries")
        if isinstance(queries, list):
            return ", ".join(str(q) for q in queries)
        return str(queries or inp.get("regex") or super().search_text(name, inp))


_ROO_LINE = re.compile(r"^\s*(\d+) \| ", re.M)


class RooCode(Cline):
    """Roo Code (RooCodeInc/Roo-Code at commit b867ec914575, its last before the project was
    archived on 2026-05-15: packages/types/src/tool.ts). Reads come back as "File: path" and
    "N | code" lines, searches as "N | text" lines under "# path" headers, so content-mode
    search rules stay off.
    """

    KINDS: ClassVar[dict[str, str]] = {
        "read_file": "read",
        "write_to_file": "edit",
        "apply_diff": "edit",
        "edit": "edit",
        "edit_file": "edit",
        "search_and_replace": "edit",
        "search_replace": "edit",
        "apply_patch": "edit",
        "search_files": "search",
        "list_files": "search",
        "codebase_search": "search",
        "execute_command": "shell",
        "new_task": "subagent",
    }

    def read_span(self, text):
        """Read results number their lines as `N | text`."""
        numbers = _ROO_LINE.findall(text)
        return (int(numbers[0]), int(numbers[-1])) if numbers else None

    def search_text(self, name, inp):
        """Roo Code's codebase search takes a `query`."""
        return str(inp.get("query") or super().search_text(name, inp))


CLAUDE_CODE = ClaudeCode("claude-code")
CODEX = Codex("codex")
OPENCODE = OpenCode("opencode")
GEMINI_CLI = GeminiCli("gemini-cli")
CLINE = Cline("cline")
ROO_CODE = RooCode("roo-code")
GENERIC = Profile("generic")

PROFILES = {p.name: p for p in (CLAUDE_CODE, CODEX, OPENCODE, GEMINI_CLI, CLINE, ROO_CODE, GENERIC)}


def for_client(adapter_name: str, user_agent: str | None, agent: str | None = None) -> Profile:
    """The profile for a request: the launch's agent when lightfoot started it, else the user agent."""
    if agent == "opencode":
        return OPENCODE
    if agent == "cline":
        return CLINE
    if agent == "roo":
        return ROO_CODE
    if agent == "gemini" or adapter_name == "gemini":
        return GEMINI_CLI
    ua = (user_agent or "").lower()
    if "codex" in ua:
        return CODEX
    if "claude-cli" in ua or "claude-code" in ua or adapter_name == "anthropic":
        return CLAUDE_CODE
    if adapter_name == "responses":
        return CODEX
    return GENERIC
