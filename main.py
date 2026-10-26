"""Service entry point: `python main.py` (this is what systemd ExecStart runs).

All router code lives in the `backend` package; this file only binds a process to it.
"""
from backend.app import app, serve  # noqa: F401  (app re-exported for `uvicorn main:app`)

__all__ = ["app", "serve"]

if __name__ == "__main__":
    serve()
