import os
from functools import lru_cache

import voyageai

EMBED_MODEL = "voyage-code-3"
EMBED_DIM = 1024


@lru_cache(maxsize=1)
def _client() -> voyageai.Client:
    api_key = os.environ.get("VOYAGE_API_KEY")
    if not api_key:
        raise RuntimeError("VOYAGE_API_KEY is not set")
    return voyageai.Client(api_key=api_key)


def embed(text: str, *, input_type: str = "document") -> list[float]:
    """Embed a single string. input_type is 'document' or 'query'."""
    result = _client().embed(
        [text],
        model=EMBED_MODEL,
        input_type=input_type,
        output_dimension=EMBED_DIM,
    )
    return result.embeddings[0]
