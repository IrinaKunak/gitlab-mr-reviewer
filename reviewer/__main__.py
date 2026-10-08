"""The service's single entry point: `python -m reviewer` (Dockerfile CMD).

Config is loaded and validated BEFORE uvicorn starts, so a bad .env stops the
container with a readable message instead of a failed-lifespan traceback.
`uvicorn w-server:app` keeps working through the w-server.py shim."""

import uvicorn

from .bootstrap import create_app, load_config


def main() -> None:
    uvicorn.run(create_app(load_config()), host="0.0.0.0", port=5000)


if __name__ == "__main__":
    main()
