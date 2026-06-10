"""Entrypoint shim — keeps `uvicorn w-server:app` working (Dockerfile, start.sh, docs).

The v1 monolith was refactored into the `reviewer/` package; see
plans/2026-06-11-v2-architecture.md.
"""

from reviewer.server import app  # noqa: F401

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=5000)
