"""Per-session routing of sys.stdout / sys.stderr.

Every REPL session (and every Textual-side ``/py`` invocation) runs in the
same process, so the usual trick of swapping ``sys.stdout`` doesn't work:
two sessions would trample each other. Instead ``sys.stdout`` and
``sys.stderr`` are replaced once, at boot, by :class:`Router` objects that
look up the destination in a context variable. Threads and asyncio tasks
started from a session inherit that context, so their output follows the
session that started them. Anything without a session goes to the log.
"""

from __future__ import annotations

import contextvars
import io
import logging
import threading

current_stream: contextvars.ContextVar = contextvars.ContextVar("textual_repl_stream", default=None)


class LogStream(io.TextIOBase):
    """Line-buffered stream that forwards complete lines to a logger."""

    def __init__(self, logger: logging.Logger, level: int):
        self._logger = logger
        self._level = level
        self._buffer = ""
        self._lock = threading.Lock()

    def writable(self) -> bool:
        return True

    def write(self, text: str) -> int:
        with self._lock:
            self._buffer += text
            *lines, self._buffer = self._buffer.split("\n")
        for line in lines:
            self._logger.log(self._level, "%s", line)
        return len(text)

    def flush(self) -> None:
        with self._lock:
            line, self._buffer = self._buffer, ""
        if line:
            self._logger.log(self._level, "%s", line)


class Router(io.TextIOBase):
    """``sys.stdout`` replacement that writes to the current session's stream."""

    def __init__(self, fallback: io.TextIOBase):
        self._fallback = fallback

    def _target(self):
        stream = current_stream.get()
        if stream is None or stream.closed:
            return self._fallback
        return stream

    @property
    def encoding(self) -> str:
        return "utf-8"

    @property
    def errors(self) -> str:
        return "replace"

    def writable(self) -> bool:
        return True

    def isatty(self) -> bool:
        return self._target().isatty()

    def fileno(self) -> int:
        raise io.UnsupportedOperation("REPL output is not backed by a file descriptor")

    def write(self, text: str) -> int:
        target = self._target()
        try:
            return target.write(text)
        except Exception:
            if target is self._fallback:
                raise
            return self._fallback.write(text)

    def flush(self) -> None:
        try:
            self._target().flush()
        except Exception:
            pass


class CallbackStream(io.TextIOBase):
    """Stream that hands every write to a callable (until closed)."""

    def __init__(self, callback, *, tty: bool = False):
        self._callback = callback
        self._tty = tty

    def writable(self) -> bool:
        return True

    def isatty(self) -> bool:
        return self._tty

    def write(self, text: str) -> int:
        if self.closed:
            raise ValueError("write to closed stream")
        if text:
            self._callback(text)
        return len(text)


class BufferStream(io.StringIO):
    """In-memory stream used to capture output (e.g. for ``/py``)."""

    def isatty(self) -> bool:
        return False
