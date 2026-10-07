"""The service's single entry point: `python -m reviewer` (Dockerfile CMD).

`uvicorn w-server:app` keeps working through the w-server.py shim."""

import uvicorn

from .server import app


def main() -> None:
    uvicorn.run(app, host="0.0.0.0", port=5000)


if __name__ == "__main__":
    main()
