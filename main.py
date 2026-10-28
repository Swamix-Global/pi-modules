"""Service entry point: `python main.py` (this is what systemd ExecStart runs).

The backend namespace contains exactly app.py, config.py and utils.py.
"""
import sys

sys.dont_write_bytecode = True

from backend.app import app, serve  # noqa: F401  (app re-exported for `uvicorn main:app`)

__all__ = ["app", "serve"]

if __name__ == "__main__":
    serve()
