"""Import shim — keeps `uvicorn w-server:app` working for existing deploy
scripts and docs. The entry point is `python -m reviewer` (reviewer/__main__.py);
the v1 monolith became the `reviewer/` package (plans/2026-06-11-v2-architecture.md).
"""

from reviewer.server import app  # noqa: F401
