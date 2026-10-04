"""REPL windows: Textual utility windows that REPL output can be sent to.

Every front end has a window name (a context variable, so handlers and
tasks inherit it): interactive SSH sessions get "Python REPL 1", "Python
REPL 2"... and everything else shares "Python REPL". Windows are created on
demand, per connection, as utility windows, which Textual never sends to
the server, saves, or logs.

Names contain spaces so they can't collide with a real channel or nick.
"""

from __future__ import annotations

import contextvars
import logging
import threading
import time

from . import mainthread

log = logging.getLogger(__name__)

SHARED_WINDOW = "Python REPL"
CHANNEL_TYPE_UTILITY = 2

current_window: contextvars.ContextVar = contextvars.ContextVar("textual_repl_window", default=SHARED_WINDOW)

_lock = threading.Lock()
_session_numbers: set[int] = set()


def allocate_session_window() -> tuple[str, callable]:
    """Reserve the lowest free "Python REPL <n>" name; returns (name, release)."""
    with _lock:
        number = 1
        while number in _session_numbers:
            number += 1
        _session_numbers.add(number)

    def release():
        with _lock:
            _session_numbers.discard(number)

    return f"{SHARED_WINDOW} {number}", release


def window(client_objc, name: str | None = None):
    """Find or create the REPL window ``name`` on a client. Main thread only."""
    name = name or current_window.get()
    channel = client_objc.findChannel_(name)
    if channel is None:
        channel = client_objc.findChannelOrCreate_asType_(name, CHANNEL_TYPE_UTILITY)
    if channel is None or not channel.isUtility():
        return None
    return channel


def print_lines(client_objc, lines: list[str], name: str | None = None) -> None:
    """Print lines into a REPL window (falls back to the server console)."""
    name = name or current_window.get()

    def run():
        channel = window(client_objc, name)
        for line in lines:
            if channel is not None:
                client_objc.printDebugInformation_inChannel_(line, channel)
            else:
                client_objc.printDebugInformationToConsole_(line)

    mainthread.perform(run)


def _duration(seconds: int) -> str:
    parts = []
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            parts.append(f"{seconds // size}{unit}")
            seconds %= size
    parts.append(f"{seconds}s")
    return " ".join(parts)


def format_reply(event) -> str | None:
    """Human readable form of a reply to a REPL request (None to skip it)."""
    p = event.params
    nick = p[1] if len(p) > 1 else ""
    command = event.command
    if command == "311" and len(p) >= 6:
        return f"{nick} is {p[2]}@{p[3]} ({p[5]})"
    if command == "312" and len(p) >= 4:
        return f"{nick} is connected to {p[2]} ({p[3]})"
    if command == "317" and len(p) >= 4 and p[2].isdigit() and p[3].isdigit():
        signon = time.strftime("%Y-%m-%d %H:%M", time.localtime(int(p[3])))
        return f"{nick} has been idle for {_duration(int(p[2]))}, signed on {signon}"
    if command == "319" and len(p) >= 3:
        return f"{nick} is in {p[2]}"
    if command == "330" and len(p) >= 3:
        return f"{nick} is logged in as {p[2]}"
    if command == "301" and len(p) >= 3:
        return f"{nick} is away: {p[2]}"
    if command in ("318", "315", "323", "366", "368"):  # end-of-list markers
        return None
    if len(p) > 2:
        return f"{p[1]}: {' '.join(p[2:])}"
    return " ".join(p[1:]) or command
