"""A deadline guard so a blocking production call fails the suite instead of hanging it.

A test that never returns reports nothing: CI is killed by its job timeout with no failing
test named, which is strictly less useful than a failure. Calls that acquire a file lock are
made through `within_deadline`, so a lock that blocks against itself surfaces as `Deadlock`.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")

DEFAULT_DEADLINE_S = 5.0


class Deadlock(AssertionError):
    """Raised when a guarded call does not return inside its deadline."""


def within_deadline(call: Callable[[], T], *, seconds: float = DEFAULT_DEADLINE_S) -> T:
    """Return `call()`'s result, or raise `Deadlock` if it does not return in time.

    The worker is a daemon thread: a call blocked on a lock it can never acquire is left
    parked there rather than joined, which keeps the reporting of every later test intact.
    """
    outcome: list[T] = []
    failure: list[BaseException] = []

    def run() -> None:
        try:
            outcome.append(call())
        except BaseException as exc:  # noqa: BLE001 - re-raised on the calling thread
            failure.append(exc)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(seconds)
    if worker.is_alive():
        raise Deadlock(f"call did not return within {seconds}s")
    if failure:
        raise failure[0]
    return outcome[0]
