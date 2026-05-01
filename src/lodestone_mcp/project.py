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
    """
    override = os.environ.get("LODESTONE_PROJECT_ID")
    if override:
        return override, override
    cwd = cwd or Path.cwd()
    label = _git_remote(cwd) or str(cwd.resolve())
    project_id = hashlib.sha256(label.encode()).hexdigest()[:16]
    return project_id, label


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
