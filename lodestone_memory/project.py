import hashlib
import os
import subprocess
from pathlib import Path


def derive_project_id(cwd: Path | None = None) -> tuple[str, str]:
    """Returns (project_id, label).

    Order of precedence:
      1. LODESTONE_PROJECT_ID env var (used by evals to scope a sandbox)
      2. git remote URL (stable across clones/worktrees)
      3. absolute path of cwd

    Resilient to a deleted/recreated cwd — when a user `rm -rf`s the
    project directory while the MCP server process is running, the
    process holds an open fd for the (now-deleted) inode and
    `Path.cwd()` raises `FileNotFoundError([Errno 2])` with no filename.
    Without a fallback, every recall/remember/list_recent fails with
    that opaque message until the server is restarted. We fall back to
    `$PWD` (set by the shell and not tied to the inode), then to a
    stable placeholder, so recall keeps working through the disruption.
    """
    override = os.environ.get("LODESTONE_PROJECT_ID")
    if override:
        return override, override
    if cwd is None:
        cwd = _resolve_cwd_safely()
    label = _git_remote(cwd) or _safe_str(cwd)
    project_id = hashlib.sha256(label.encode()).hexdigest()[:16]
    return project_id, label


def _resolve_cwd_safely() -> Path:
    """Return Path.cwd(), or a sensible fallback if the cwd inode is gone."""
    try:
        return Path.cwd()
    except (FileNotFoundError, OSError):
        pwd = os.environ.get("PWD")
        if pwd:
            return Path(pwd)
        return Path("/lodestone-unknown-cwd")


def _safe_str(cwd: Path) -> str:
    """str(cwd.resolve()) when the path is real, plain str(cwd) otherwise."""
    try:
        return str(cwd.resolve())
    except (FileNotFoundError, OSError):
        return str(cwd)


def _git_remote(cwd: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), "config", "--get", "remote.origin.url"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return None
    url = result.stdout.strip()
    return url if result.returncode == 0 and url else None
