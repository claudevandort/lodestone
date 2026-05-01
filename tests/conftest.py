import hashlib
import math
from pathlib import Path

import pytest

from lodestone_mcp import db, embeddings, memory


def _fake_embed(text: str, *, input_type: str = "document") -> list[float]:
    """Deterministic bag-of-tokens embedding.

    Identical text → identical vector (cosine sim 1.0).
    Overlapping vocab → high cosine similarity.
    Disjoint vocab → orthogonal (cosine sim ~0).

    Stable across processes (uses md5, not Python's randomized hash()).
    """
    dim = embeddings.EMBED_DIM
    vec = [0.0] * dim
    for token in text.lower().split():
        idx = int(hashlib.md5(token.encode()).hexdigest(), 16) % dim
        vec[idx] += 1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


@pytest.fixture
def temp_db(tmp_path: Path):
    conn = db.open_db(tmp_path / "test.db")
    yield conn
    conn.close()


@pytest.fixture
def fake_embed(monkeypatch):
    monkeypatch.setattr(memory.embeddings, "embed", _fake_embed)
    return _fake_embed


class _Clock:
    def __init__(self, t: int):
        self.t = t

    def set(self, t: int) -> None:
        self.t = t

    def advance(self, seconds: int) -> None:
        self.t += seconds


@pytest.fixture
def clock(monkeypatch):
    c = _Clock(1_700_000_000)  # arbitrary fixed epoch ~Nov 2023
    monkeypatch.setattr(memory, "_now", lambda: c.t)
    return c
