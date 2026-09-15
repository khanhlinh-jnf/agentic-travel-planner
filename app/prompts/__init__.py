"""Versioned chat prompts; business services depend only on registry()."""

from app.prompts.registry import registry

__all__ = ["registry"]
