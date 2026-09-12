"""Process-local external-call accounting shared by graph and on-demand endpoints."""

from __future__ import annotations

_external_calls: dict[str, int] = {}


def reset(thread_id: str) -> None:
    _external_calls[thread_id] = 0


def record(thread_id: str, amount: int) -> int:
    _external_calls[thread_id] = _external_calls.get(thread_id, 0) + max(0, amount)
    return _external_calls[thread_id]


def total(thread_id: str) -> int:
    return _external_calls.get(thread_id, 0)
