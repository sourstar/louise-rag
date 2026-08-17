"""The coordinator service (architecture-v2.md §7).

A separate, standalone FastAPI app that holds no bulk data: it leases work,
accepts signed bundles, tracks who provided what, and aggregates what consumers
want. A node can run with no coordinator at all, or point at somebody else's —
nothing in app/api.py depends on this package.

    python -m app.coordinator.service        # or uvicorn app.coordinator.service:app
"""
from .service import create_app
from .store import connect

__all__ = ["create_app", "connect"]
