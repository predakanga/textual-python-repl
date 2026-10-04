"""Helpers for running code on Textual's main thread.

Textual mutates its IRC state on the main thread, so the wrappers in
:mod:`textual_repl.irc` funnel every call through :func:`call` (wait for a
result) or :func:`perform` (fire and forget when it's unsafe to wait).

Server input filters run synchronously on a connection thread while
Textual waits for them. Blocking there on the main thread could deadlock,
so inside a filter :func:`call` runs inline and :func:`perform` becomes
asynchronous.
"""

from __future__ import annotations

import contextlib
import logging
import threading

from Foundation import NSOperationQueue, NSThread

log = logging.getLogger(__name__)

_local = threading.local()


def is_main_thread() -> bool:
    return bool(NSThread.isMainThread())


def in_filter() -> bool:
    return getattr(_local, "filter_depth", 0) > 0


@contextlib.contextmanager
def filter_context():
    _local.filter_depth = getattr(_local, "filter_depth", 0) + 1
    try:
        yield
    finally:
        _local.filter_depth -= 1


def call(fn, *args, **kwargs):
    """Run ``fn(*args, **kwargs)`` on the main thread and return its result."""
    if is_main_thread() or in_filter():
        return fn(*args, **kwargs)

    done = threading.Event()
    box = {}

    def block():
        try:
            box["result"] = fn(*args, **kwargs)
        except BaseException as exc:
            box["error"] = exc
        finally:
            done.set()

    NSOperationQueue.mainQueue().addOperationWithBlock_(block)

    # Poll so that the waiting thread stays interruptible (Ctrl-C in a
    # session is delivered as an asynchronous exception).
    while not done.wait(0.1):
        pass

    if "error" in box:
        raise box["error"]
    return box.get("result")


def perform(fn, *args, **kwargs) -> None:
    """Run ``fn`` on the main thread. Waits, except when that is unsafe."""
    if in_filter():

        def block():
            # An exception escaping into Objective-C would take Textual down.
            try:
                fn(*args, **kwargs)
            except BaseException:
                log.exception("Deferred main thread call failed")

        NSOperationQueue.mainQueue().addOperationWithBlock_(block)
    else:
        call(fn, *args, **kwargs)
