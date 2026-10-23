"""Compatibility entry point: `uvicorn axongate_gateway:app` keeps working."""

from axongate.app import app

__all__ = ["app"]
