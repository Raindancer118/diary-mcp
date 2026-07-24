"""
Embedding support for semantic memory search.

Uses fastembed (ONNX, CPU, no torch) with a lightweight multilingual model so
German + English memories embed into the same space. The model is loaded lazily
on first use and cached for the lifetime of the process. If fastembed or the
model is unavailable, embedding degrades gracefully to None and the caller falls
back to lexical search.

Storage: embeddings are plain REAL[] in Postgres; similarity is computed in
Python (brute-force cosine), which is millisecond-fast for the expected scale
(hundreds to low thousands of memories) and needs no pgvector extension.
"""
from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

_log = logging.getLogger(__name__)

DEFAULT_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
EMBED_DIM = 384

# fastembed defaults to caching the ~470MB ONNX model under
# tempfile.gettempdir()/fastembed_cache when FASTEMBED_CACHE_PATH is unset. On
# systems where /tmp is tmpfs (wiped every reboot — the default on this CachyOS
# box), that means the model gets silently re-downloaded from HuggingFace on the
# first embed() call of every fresh boot: a multi-second-to-minutes stall on the
# hot path of memory_search/memory_upsert. Point it at a persistent XDG-style
# cache dir instead, unless the user already configured one explicitly.
_DEFAULT_CACHE_DIR = Path.home() / ".cache" / "fastembed"
os.environ.setdefault("FASTEMBED_CACHE_PATH", str(_DEFAULT_CACHE_DIR))

_model = None
_unavailable = False
_load_lock = threading.Lock()


def model_name() -> str:
    return os.environ.get("DIARY_EMBED_MODEL", DEFAULT_MODEL)


def is_available() -> bool:
    """True if embeddings can be produced (model loads). Cached after first check."""
    return _get_model() is not None


def _get_model():
    global _model, _unavailable
    if _model is not None:
        return _model
    if _unavailable:
        return None
    with _load_lock:
        # Re-check inside the lock: another thread (e.g. the startup warmup
        # thread) may have finished loading while we were waiting for it.
        if _model is not None:
            return _model
        if _unavailable:
            return None
        try:
            from fastembed import TextEmbedding  # heavy import — defer to first use
            _model = TextEmbedding(model_name())
            _log.info("Loaded embedding model %s", model_name())
            return _model
        except Exception as exc:  # noqa: BLE001 — any failure → graceful fallback
            _log.warning("Embedding model unavailable (%s); semantic search disabled", exc)
            _unavailable = True
            return None


def embed(text: str) -> list[float] | None:
    """Embed a single text. Returns None if embeddings are unavailable."""
    if not text or not text.strip():
        return None
    model = _get_model()
    if model is None:
        return None
    try:
        vec = next(iter(model.embed([text])))
        return [float(x) for x in vec]
    except Exception as exc:  # noqa: BLE001
        _log.warning("Embedding failed: %s", exc)
        return None


def embed_many(texts: list[str]) -> list[list[float] | None]:
    """Embed many texts in one batch. Returns a list aligned with the input."""
    model = _get_model()
    if model is None:
        return [None] * len(texts)
    try:
        return [[float(x) for x in v] for v in model.embed(texts)]
    except Exception as exc:  # noqa: BLE001
        _log.warning("Batch embedding failed: %s", exc)
        return [None] * len(texts)


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two equal-length vectors (pure Python, no numpy dep)."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / ((na ** 0.5) * (nb ** 0.5))


def normalize(vec: list[float]) -> list[float]:
    """L2-normalize a vector. Used to hoist norm computation out of O(n^2) pairwise
    similarity loops (e.g. memory_infer_links) — cosine of two unit vectors is a
    plain dot product, so the sqrt/division work happens once per vector instead
    of once per pair."""
    norm = sum(x * x for x in vec) ** 0.5
    if norm == 0.0:
        return vec
    return [x / norm for x in vec]


def dot(a: list[float], b: list[float]) -> float:
    """Dot product of two equal-length vectors (pure Python, no numpy dep).

    Returns 0.0 for empty or length-mismatched inputs — mirrors cosine()'s guard,
    since a bare zip() would otherwise silently truncate to the shorter vector
    instead of signaling "these embeddings aren't comparable" (e.g. two nodes
    embedded under different DIARY_EMBED_MODEL dimensions).
    """
    if not a or not b or len(a) != len(b):
        return 0.0
    return sum(x * y for x, y in zip(a, b))
