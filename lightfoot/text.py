"""Pure text helpers shared by the rules and the validator."""

from __future__ import annotations

import bisect
import json
import re

ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")

# Errs toward matching: a line kept by mistake costs a few tokens, a dropped
# error line can cost a wrong answer.
ERROR_RE = re.compile(
    r"(?im)(?:error|exception)s?\b"  # error, errors, TypeError, NullPointerException
    r"|\b(?:traceback|fail|fails|failed|failure|failures|failing|fatal|panic|panicked|assert|assertion"
    r"|segmentation fault|abort|aborted|denied|cannot|unable|undefined reference|not found|err!?)\b"
    r"|[✗✘❌]|^E\s"
)

_BRAILLE_SPINNER = re.compile(r"^\s*[⠁-⣿]\s")
_TQDM = re.compile(r"\d{1,3}(?:\.\d+)?%\s*\|[^|]*\|")
_BRACKET_BAR = re.compile(r"\[[=#>\-. ]{10,}\]")
_BLOCK_BAR = re.compile(r"[█▉▊▋▌▍▎▏░▒▓━■]{5,}")
_PERCENT = re.compile(r"\d{1,3}(?:\.\d+)?\s?%")

LINE_NUMBER_RE = re.compile(r"^\s*(\d+)(?:\t|→)", re.M)

LONG_LINE = 2000
LONG_LINE_KEEP = 1000


def tokens(text: str) -> int:
    """Cheap token estimate (about 4 characters per token)."""
    return (len(text) + 3) // 4


def is_error_line(line: str) -> bool:
    """Whether a line looks like an error, failure, traceback or the like.

    Rules that trim output keep every such line.
    """
    return bool(ERROR_RE.search(line))


# Every ERROR_RE match contains one of these (lower-cased) substrings, so a fast str.find
# pass finds the candidate lines and the regex only confirms them.
_ERROR_KEYWORDS = (
    "err",
    "exception",
    "traceback",
    "fail",
    "fatal",
    "panic",
    "assert",
    "segmentation fault",
    "abort",
    "denied",
    "cannot",
    "unable",
    "undefined reference",
    "not found",
    "✗",
    "✘",
    "❌",
)
_E_LINE = re.compile(r"(?m)^E\s")


def error_line_indices(lines: list[str]) -> set[int]:
    """Indices of the lines that ERROR_RE matches."""
    text = "\n".join(lines)
    low = text.lower()
    if len(low) != len(text):  # rare case-mapping that changes length: check line by line
        return {i for i, line in enumerate(lines) if ERROR_RE.search(line)}
    starts = [0]
    for line in lines[:-1]:
        starts.append(starts[-1] + len(line) + 1)
    candidates: set[int] = set()
    for keyword in _ERROR_KEYWORDS:
        pos = low.find(keyword)
        while pos != -1:
            line_no = bisect.bisect_right(starts, pos) - 1
            candidates.add(line_no)
            pos = low.find(keyword, starts[line_no + 1]) if line_no + 1 < len(starts) else -1
    for match in _E_LINE.finditer(text):
        candidates.add(bisect.bisect_right(starts, match.start()) - 1)
    return {i for i in candidates if ERROR_RE.search(lines[i])}


def normalize_terminal(text: str) -> str:
    """Strip ANSI escapes and resolve carriage-return overwrites, as a terminal would show them."""
    if "\x1b" in text:  # most output has no escapes, and the check is much faster than the regex
        text = ANSI_RE.sub("", text)
    if "\r" not in text:
        return text
    text = text.replace("\r\n", "\n")
    out = []
    for line in text.split("\n"):
        if "\r" in line:
            segments = [s for s in line.split("\r") if s.strip()]
            line = segments[-1] if segments else ""
        out.append(line)
    return "\n".join(out)


def is_progress_line(line: str) -> bool:
    """Whether a line is a spinner or progress bar, which the noise rule collapses. Error lines never count."""
    if _BRAILLE_SPINNER.search(line) or _TQDM.search(line):
        progress = True
    elif _BRACKET_BAR.search(line) or _BLOCK_BAR.search(line):
        progress = bool(_PERCENT.search(line)) or len(line.strip()) < 120
    else:
        return False
    return progress and not is_error_line(line)


def denoise_lines(lines: list[str]) -> list[str]:
    """Drop all but the last line of each run of progress lines; collapse repeated lines."""
    progress = [is_progress_line(line) for line in lines]
    kept = [line for i, line in enumerate(lines) if not (progress[i] and i + 1 < len(lines) and progress[i + 1])]

    out: list[str] = []
    i = 0
    while i < len(kept):
        j = i
        while j + 1 < len(kept) and kept[j + 1] == kept[i]:
            j += 1
        run = j - i + 1
        if not kept[i].strip():
            out.append(kept[i])
            if run >= 2:
                out.append(kept[i])  # keep at most two blank lines
        elif run >= 3:
            out.append(kept[i])
            out.append(f"[lightfoot: previous line repeated {run - 1} more times]")
        else:
            out.extend(kept[i : j + 1])
        i = j + 1
    return out


def truncate_long_line(line: str) -> str:
    """Cut a very long line to its start and a note, unless it is an error line."""
    if len(line) <= LONG_LINE or is_error_line(line):
        return line
    return f"{line[:LONG_LINE_KEEP]} … [lightfoot: {len(line) - LONG_LINE_KEEP} more characters]"


def select_lines(lines: list[str], head: int, tail: int, context: int) -> list[int]:
    """Indices to keep: first `head`, last `tail`, every error line with `context` lines around it."""
    n = len(lines)
    keep = set(range(min(head, n)))
    keep.update(range(max(0, n - tail), n))
    for i in error_line_indices(lines):
        keep.update(range(max(0, i - context), min(n, i + context + 1)))
    return sorted(keep)


def render_selection(lines: list[str], keep: list[int]) -> str:
    """Kept lines (denoised per segment) with markers naming the omitted 1-based line ranges."""
    out: list[str] = []
    segment: list[str] = []
    prev = -1
    for i in keep:
        if i != prev + 1:
            out.extend(denoise_lines(segment))
            segment = []
            out.append(f"… [lightfoot: lines {prev + 2}-{i} omitted] …")
        segment.append(truncate_long_line(lines[i]))
        prev = i
    out.extend(denoise_lines(segment))
    if prev + 1 < len(lines):
        out.append(f"… [lightfoot: lines {prev + 2}-{len(lines)} omitted] …")
    return "\n".join(out)


# A JSON string token, or a run of the four whitespace characters JSON allows between tokens.
# Substituting r"\1" keeps strings and drops whitespace (an unmatched group becomes "").
_JSON_STRING_OR_SPACE = re.compile(r'("[^"\\]*(?:\\.[^"\\]*)*")|[ \t\n\r]+')
MAX_JSON_CHARS = 4_000_000


def minify_json(text: str) -> str | None:
    """`text` without the whitespace between JSON tokens, or None if it is not a JSON document.

    Lossless by construction: strings, numbers, key order and duplicate keys are kept byte for
    byte, because nothing is re-serialized; only whitespace outside strings is removed. The
    result is parsed again and must equal the original, so a bug here can never change a value.
    """
    stripped = text.strip()
    if len(stripped) > MAX_JSON_CHARS or not stripped or stripped[0] not in "{[":
        return None
    try:
        value = json.loads(stripped)
    except ValueError:
        return None
    minified = _JSON_STRING_OR_SPACE.sub(r"\1", stripped)
    if len(minified) >= len(text):
        return None
    try:
        if json.loads(minified) != value:
            return None
    except ValueError:
        return None
    return minified


def line_number_range(text: str) -> tuple[int, int] | None:
    """First and last line number of a Read result in cat -n format."""
    numbers = LINE_NUMBER_RE.findall(text)
    if not numbers:
        return None
    return int(numbers[0]), int(numbers[-1])


_GREP_PATH = re.compile(r"^((?:[A-Za-z]:[\\/])?[^:\n]+?):(?:\d+:)?")


def grep_line_path(line: str) -> str | None:
    """Path prefix of a Grep content-mode line, or None when the line has no plausible path."""
    match = _GREP_PATH.match(line)
    if not match:
        return None
    path = match.group(1)
    if not path or path != path.strip():
        return None
    if not ("/" in path or "\\" in path or "." in path):
        return None
    return path


def norm_path(path: str) -> str:
    """Comparable form of a path: forward slashes, lower case, no leading ./"""
    p = path.strip().replace("\\", "/").lower()
    while p.startswith("./"):
        p = p[2:]
    return p.rstrip("/")


def same_file(opened: str, hit: str) -> bool:
    """Whether two paths name the same file, allowing one to be relative to the other."""
    a, b = norm_path(opened), norm_path(hit)
    return a == b or a.endswith("/" + b) or b.endswith("/" + a)


_TOKEN_RE = re.compile(r"[\w./\\:\-]{8,}")
_CAMEL_RE = re.compile(r"[a-z][A-Z]")


def distinctive_identifiers(text: str, limit: int = 40, min_len: int = 8, scan_chars: int = 8_000) -> list[str]:
    """Paths and compound identifiers (snake_case, dotted, camelCase) likely to reappear if
    later messages discuss this content."""
    found: list[str] = []
    seen: set[str] = set()

    def add(token: str) -> None:
        if len(token) >= min_len and token not in seen:
            seen.add(token)
            found.append(token)

    if len(text) > scan_chars:
        text = text[: scan_chars // 2] + "\n" + text[-scan_chars // 2 :]
    for match in _TOKEN_RE.finditer(text):
        token = match.group(0).strip(".:-/\\")
        if len(token) < min_len:
            continue
        if "/" in token or "\\" in token:
            add(token)
            add(re.split(r"[\\/]", token)[-1])
        elif (token[0].isalpha() or token[0] == "_") and (
            "_" in token[1:-1] or "." in token[1:-1] or _CAMEL_RE.search(token)
        ):
            add(token)
        if len(found) >= limit:
            break
    return found[:limit]
