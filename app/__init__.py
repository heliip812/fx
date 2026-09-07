"""Dash application for the FX gamma desk  [dev-owned, contract section 6]."""
from __future__ import annotations

__all__ = ["create_app"]


def create_app(*a, **kw):
    """Lazy re-export so ``import app`` stays cheap."""
    from .main import create_app as _f
    return _f(*a, **kw)
