"""The benchmark plumbing: task files with repo_url, setup and {task_dir}, `eval check`, and bench/benchenv.py."""

import subprocess
import sys
from pathlib import Path

import pytest

from lightfoot import evaluate

ROOT = Path(__file__).resolve().parent.parent
BENCHENV = ROOT / "bench" / "benchenv.py"


def git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-q")
    git(path, "config", "user.email", "t@example.com")
    git(path, "config", "user.name", "t")
    (path / "tests").mkdir()
    (path / "tests" / "test_a.py").write_text("def test_a():\n    assert True\n")
    (path / "mod.py").write_text("x = 1\n")
    git(path, "add", ".")
    git(path, "commit", "-qm", "init")
    return path


def head(path):
    return subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()


def write_tasks(tmp_path, body):
    path = tmp_path / "tasks" / "tasks.toml"
    path.parent.mkdir(exist_ok=True)
    path.write_text('agent = "claude"\npermission_mode = "acceptEdits"\n' + body, encoding="utf-8")
    return path


def test_task_dir_is_filled_in_and_repo_url_needs_a_commit(tmp_path):
    path = write_tasks(
        tmp_path, '[[task]]\nid = "a"\nrepo = "."\nprompt = "p"\nsetup = "python {task_dir}/x.py"\ntest = "t"\n'
    )
    task = evaluate.load_tasks(path)["task"][0]
    assert task["setup"] == f"python {path.parent.resolve()}/x.py"
    bad = write_tasks(tmp_path, '[[task]]\nid = "b"\nrepo_url = "https://example.com/r"\nprompt = "p"\ntest = "t"\n')
    with pytest.raises(ValueError, match="pinned commit"):
        evaluate.load_tasks(bad)
    none = write_tasks(tmp_path, '[[task]]\nid = "c"\nprompt = "p"\ntest = "t"\n')
    with pytest.raises(ValueError, match="repo or repo_url"):
        evaluate.load_tasks(none)


def test_check_accepts_failing_tests_and_rejects_passing_ones(tmp_path, repo):
    fails = f'{sys.executable} -c "import sys; sys.exit(1)"'
    passes = f'{sys.executable} -c "pass"'
    path = write_tasks(
        tmp_path,
        f'[[task]]\nid = "good"\nrepo = "{repo.as_posix()}"\nprompt = "p"\ntest = \'{fails}\'\n'
        f'[[task]]\nid = "useless"\nrepo = "{repo.as_posix()}"\nprompt = "p"\ntest = \'{passes}\'\n'
        f'[[task]]\nid = "broken"\nrepo = "{repo.as_posix()}"\nprompt = "p"\nsetup = \'{fails}\'\ntest = \'{fails}\'\n',
    )
    out = evaluate.check(str(path))
    assert "ok good:" in out and "XX useless: the test already passes" in out and "XX broken: setup failed" in out
    listed = subprocess.run(["git", "-C", str(repo), "worktree", "list"], capture_output=True, text=True).stdout
    assert len(listed.strip().splitlines()) == 1  # every worktree was removed


def test_repo_url_is_cloned_once_and_pinned(tmp_path, repo, lightfoot_home):
    task = {"id": "t", "repo_url": repo.as_uri(), "commit": head(repo)}
    first = evaluate.repo_path(task)
    assert Path(first, "mod.py").exists() is False  # cloned without a checkout; runs use worktrees
    assert evaluate.repo_path(task) == first
    with pytest.raises(RuntimeError, match="not found"):
        evaluate.repo_path({**task, "commit": "0" * 40})


def benchenv(cwd, *args):
    return subprocess.run([sys.executable, str(BENCHENV), *args], cwd=cwd, capture_output=True, text=True)


def test_benchenv_checks(repo):
    commit = head(repo)
    assert benchenv(repo, "unchanged", commit, "tests").returncode == 0
    (repo / "tests" / "test_a.py").write_text("def test_a():\n    pass\n")
    assert benchenv(repo, "unchanged", commit, "tests").returncode == 1  # edited
    git(repo, "checkout", "--", "tests")
    (repo / "tests" / "test_new.py").write_text("")
    assert benchenv(repo, "unchanged", commit, "tests").returncode == 1  # added
    (repo / "tests" / "test_new.py").unlink()
    git(repo, "rm", "-q", "tests/test_a.py")
    git(repo, "commit", "-qm", "drop")
    assert benchenv(repo, "unchanged", commit, "tests").returncode == 1  # deleted and committed
    assert benchenv(repo, "absent", "mod.py").returncode == 1
    assert benchenv(repo, "absent", "nothing.py").returncode == 0
    pkg = repo / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "a.py").write_text("")
    assert benchenv(repo, "modules", "pkg", "2").returncode == 1
    (pkg / "b.py").write_text("")
    assert benchenv(repo, "modules", "pkg", "2").returncode == 0
    assert benchenv(repo, "bogus").returncode == 2


def test_the_published_task_file_loads():
    spec = evaluate.load_tasks(ROOT / "bench" / "tasks.toml")
    assert {t["id"] for t in spec["task"]} == {"boltons-types", "toolz-types", "toolz-split"}
    for t in spec["task"]:
        assert len(t["commit"]) == 40 and t["repo_url"].startswith("https://github.com/")
        assert "{task_dir}" not in t["setup"] + t["test"] and str(BENCHENV.parent) in t["test"]
        assert "unchanged" in t["test"]  # no task can be passed by editing its tests
