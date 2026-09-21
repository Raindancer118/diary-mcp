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

import diary_embed_ipc as _ipc

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

# Shared-embedding IPC role: "server" (owns the model, serves others),
# "client" (routes through another process's server), or "standalone"
# (IPC disabled, always loads its own copy). See diary_embed_ipc.py.
_role: str | None = None
_role_lock = threading.Lock()
_server: _ipc.EmbedServer | None = None


def model_name() -> str:
    return os.environ.get("DIARY_EMBED_MODEL", DEFAULT_MODEL)


def is_available() -> bool:
    """True if embeddings can be produced (model loads, or a remote server is
    reachable)."""
    role = _ensure_role()
    if role == "client":
        if _ipc.request_remote(["ping"], timeout=5.0) is not None:
            return True
        _downgrade_to_standalone()
    return _get_model() is not None


def _ensure_role() -> str:
    """Elect this process's role in the shared-embedding IPC scheme, once.

    The first process on the machine to bind the shared socket becomes the
    server and loads the model for everyone; later processes become clients
    and never load their own copy unless the server later becomes
    unreachable (see embed_many's fallback)."""
    global _role, _server
    if _role is not None:
        return _role
    with _role_lock:
        if _role is not None:
            return _role
        if os.environ.get("DIARY_EMBED_NO_IPC"):
            _role = "standalone"
            return _role
        server = _ipc.EmbedServer(_compute_local)
        if server.try_start():
            _server = server
            _role = "server"
            _log.info("Elected as shared-embedding server")
        else:
            _role = "client"
            _log.info("Using shared-embedding server from another process")
        return _role


def _downgrade_to_standalone() -> None:
    """Called when the elected remote server turns out to be unreachable
    (e.g. it exited). Fall back to loading a local model for this process
    from now on, rather than retrying a dead socket on every call."""
    global _role
    _log.warning("Shared-embedding server unreachable; loading local model instead")
    _role = "standalone"


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


def _compute_local(texts: list[str]) -> list[list[float] | None]:
    """Embed texts using this process's own model (loading it if needed).
    This is the function the IPC server runs to answer remote requests, and
    what server/standalone roles fall through to locally."""
    if not texts:
        return []
    model = _get_model()
    if model is None:
        return [None] * len(texts)
    try:
        return [[float(x) for x in v] for v in model.embed(texts)]
    except Exception as exc:  # noqa: BLE001
        _log.warning("Batch embedding failed: %s", exc)
        return [None] * len(texts)


def embed(text: str) -> list[float] | None:
    """Embed a single text. Returns None if embeddings are unavailable."""
    if not text or not text.strip():
        return None
    return embed_many([text])[0]


def embed_many(texts: list[str]) -> list[list[float] | None]:
    """Embed many texts in one batch. Returns a list aligned with the input.

    Routes through the shared-embedding server when this process is a client
    (see _ensure_role); otherwise computes locally."""
    if not texts:
        return []
    role = _ensure_role()
    if role == "client":
        vectors = _ipc.request_remote(texts)
        if vectors is not None:
            return vectors
        _downgrade_to_standalone()
    return _compute_local(texts)


def warmup() -> None:
    """Elect this process's IPC role and, only if it ends up owning the
    model (server or standalone), load it eagerly in the background so it's
    typically ready before the first real request. A client never loads its
    own copy — that's the entire point of the shared server."""
    role = _ensure_role()
    if role in ("server", "standalone"):
        _get_model()


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
