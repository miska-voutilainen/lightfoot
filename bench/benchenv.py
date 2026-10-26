"""Setup and check commands for the benchmark tasks, the same on Windows, macOS and Linux.

Each task runs in a fresh git worktree; this keeps the project's dependencies in a virtualenv
inside it (.bench-venv), so nothing is installed into your own Python.

    python benchenv.py setup PACKAGE...          create .bench-venv; install the project and PACKAGEs
    python benchenv.py run COMMAND...            run COMMAND with the virtualenv's scripts first on PATH
    python benchenv.py unchanged COMMIT PATH...  fail if PATH differs from COMMIT (edited, added or deleted)
    python benchenv.py absent PATH...            fail if a PATH exists
    python benchenv.py modules DIR N             fail unless DIR is a package with at least N modules
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import venv
from pathlib import Path

VENV = Path(".bench-venv")


def scripts() -> Path:
    """The virtualenv's executables folder: `Scripts` on Windows, `bin` elsewhere."""
    return VENV / ("Scripts" if os.name == "nt" else "bin")


def setup(packages: list[str]) -> int:
    """Create a fresh `.bench-venv` and install the project, editable, plus `packages`."""
    venv.create(VENV, with_pip=True, clear=True)
    python = str(scripts() / "python")
    return subprocess.call(
        [python, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "-e", ".", *packages]
    )


def run(command: list[str]) -> int:
    """Run `command` with the virtualenv's executables first on PATH; 127 when it can't be found."""
    env = dict(os.environ, VIRTUAL_ENV=str(VENV.resolve()))
    env["PATH"] = str(scripts().resolve()) + os.pathsep + env.get("PATH", "")
    exe = shutil.which(command[0], path=env["PATH"])
    if exe is None:
        print(f"benchenv.py: {command[0]} not found", file=sys.stderr)
        return 127
    return subprocess.call([exe, *command[1:]], env=env)


def unchanged(commit: str, paths: list[str]) -> int:
    """Fail when any of `paths` was edited, added or deleted since `commit`."""
    changed = subprocess.run(["git", "diff", "--name-only", commit, "--", *paths], capture_output=True, text=True)
    added = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard", "--", *paths], capture_output=True, text=True
    )
    files = (changed.stdout + added.stdout).split()
    if changed.returncode != 0 or files:
        print("benchenv.py: changed since the task's commit: " + (", ".join(files) or changed.stderr.strip()))
        return 1
    return 0


def absent(paths: list[str]) -> int:
    """Fail when any of `paths` still exists."""
    found = [p for p in paths if Path(p).exists()]
    if found:
        print("benchenv.py: still present: " + ", ".join(found))
    return 1 if found else 0


def modules(folder: str, count: int) -> int:
    """Fail unless `folder` is a package with at least `count` modules besides `__init__.py`."""
    path = Path(folder)
    found = [p for p in path.glob("*.py") if p.name != "__init__.py"] if (path / "__init__.py").exists() else []
    if len(found) < count:
        print(f"benchenv.py: {folder} needs __init__.py and at least {count} modules, has {len(found)}")
        return 1
    return 0


def main(argv: list[str]) -> int:
    """Dispatch one of the actions above; exit code 2 with the usage text for anything else."""
    if not argv:
        print(__doc__)
        return 2
    action, rest = argv[0], argv[1:]
    if action == "setup":
        return setup(rest)
    if action == "run" and rest:
        return run(rest)
    if action == "unchanged" and len(rest) >= 2:
        return unchanged(rest[0], rest[1:])
    if action == "absent" and rest:
        return absent(rest)
    if action == "modules" and len(rest) == 2:
        return modules(rest[0], int(rest[1]))
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
