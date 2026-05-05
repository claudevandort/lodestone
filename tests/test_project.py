"""Tests for project_id derivation, including resilience to a deleted cwd."""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from lodestone_memory.project import derive_project_id, _resolve_cwd_safely


def test_derive_project_id_uses_lodestone_project_id_env(monkeypatch):
    monkeypatch.setenv("LODESTONE_PROJECT_ID", "eval-sandbox-xyz")
    pid, label = derive_project_id()
    assert pid == "eval-sandbox-xyz"
    assert label == "eval-sandbox-xyz"


def test_resolve_cwd_safely_returns_real_cwd_when_present():
    """In the normal case, _resolve_cwd_safely is a thin Path.cwd() wrapper."""
    assert _resolve_cwd_safely() == Path.cwd()


def test_resolve_cwd_safely_falls_back_to_pwd_when_cwd_deleted(monkeypatch):
    """Regression: when the working directory has been deleted out from under
    the process (e.g. user `rm -rf`'d the project dir while the MCP server is
    running), `Path.cwd()` raises FileNotFoundError([Errno 2]) with no
    filename. Without this fallback, every recall/remember from the server
    fails with the opaque "[Errno 2] No such file or directory" message
    until the server is restarted. Reproduced from a live compass demo
    session where the user nuked and recreated the project dir between
    recalls.
    """
    original_cwd = Path.cwd()
    try:
        d = tempfile.mkdtemp(prefix="lodestone-cwd-deleted-")
        os.chdir(d)
        shutil.rmtree(d)
        # Now Path.cwd() will raise FileNotFoundError.
        monkeypatch.setenv("PWD", "/some/other/path")
        result = _resolve_cwd_safely()
        assert result == Path("/some/other/path"), \
            "must fall back to PWD env var when Path.cwd() fails"
    finally:
        os.chdir(original_cwd)


def test_resolve_cwd_safely_falls_back_to_placeholder_when_no_pwd(monkeypatch):
    """If even PWD is unset (rare but possible in stripped envs), return a
    stable placeholder path so derive_project_id stays deterministic."""
    original_cwd = Path.cwd()
    try:
        d = tempfile.mkdtemp(prefix="lodestone-cwd-deleted-")
        os.chdir(d)
        shutil.rmtree(d)
        monkeypatch.delenv("PWD", raising=False)
        result = _resolve_cwd_safely()
        assert result == Path("/lodestone-unknown-cwd"), \
            "must use a stable placeholder when both cwd and PWD are gone"
    finally:
        os.chdir(original_cwd)


def test_derive_project_id_does_not_crash_when_cwd_deleted(monkeypatch):
    """End-to-end: derive_project_id() must not raise even when cwd is gone.
    The whole point of the fallback is keeping the MCP server functional
    when this happens."""
    original_cwd = Path.cwd()
    try:
        d = tempfile.mkdtemp(prefix="lodestone-cwd-deleted-")
        os.chdir(d)
        shutil.rmtree(d)
        monkeypatch.setenv("PWD", "/home/user/some-project")
        monkeypatch.delenv("LODESTONE_PROJECT_ID", raising=False)
        # Must not raise.
        pid, label = derive_project_id()
        assert pid  # 16-char hex
        assert len(pid) == 16
        assert label  # something non-empty
    finally:
        os.chdir(original_cwd)
