"""SSH front end.

* ``ssh -p PORT host``                 interactive IPython (needs a pty)
* ``ssh -p PORT host 'irc.clients'``   run code, print output, exit status 0/1
* ``ssh -p PORT host < script.py``     run code read from stdin

Only public key authentication is accepted, against the ``authorized_keys``
file in the plugin's support folder (re-read on every connection).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import os
import socket
import threading
from typing import ClassVar

import asyncssh

from . import shell
from .output import CallbackStream

log = logging.getLogger(__name__)


def ensure_host_key(path: str) -> None:
    if os.path.exists(path):
        return
    key = asyncssh.generate_private_key("ssh-ed25519", comment="textual-python-repl")
    key.write_private_key(path)
    os.chmod(path, 0o600)
    key.write_public_key(path + ".pub")
    log.info("Generated SSH host key %s", path)


def _safe_write(process: asyncssh.SSHServerProcess, data: str) -> None:
    try:
        if not process.is_closing():
            process.stdout.write(data)
    except Exception:
        pass


def _safe_exit(process: asyncssh.SSHServerProcess, status: int) -> None:
    try:
        if not process.is_closing():
            process.exit(status)
    except Exception:
        pass


class _ServerProtocol(asyncssh.SSHServer):
    def __init__(self, server: ReplServer):
        self._server = server
        self._conn = None

    def connection_made(self, conn):
        self._conn = conn
        # Belt and braces with the SIGPIPE mask (see plugin._boot): never
        # signal the host process when a client hangs up mid-write.
        sock = conn.get_extra_info("socket")
        if sock is not None:
            sock.setsockopt(socket.SOL_SOCKET, getattr(socket, "SO_NOSIGPIPE", 0x1022), 1)
        log.info("SSH connection from %s", conn.get_extra_info("peername"))

    def connection_lost(self, exc):
        if exc:
            log.info("SSH connection lost: %s", exc)

    def begin_auth(self, username):
        try:
            keys = asyncssh.read_authorized_keys(self._server.authorized_keys_path)
        except (OSError, ValueError) as exc:
            log.warning("No usable authorized keys (%s); refusing login", exc)
            keys = asyncssh.import_authorized_keys("")
        self._conn.set_authorized_keys(keys)
        return True

    def password_auth_supported(self):
        return False

    def kbdint_auth_supported(self):
        return False


_loop: asyncio.AbstractEventLoop | None = None
_pool: concurrent.futures.ThreadPoolExecutor | None = None
_loop_lock = threading.Lock()


def _ssh_loop() -> asyncio.AbstractEventLoop:
    """One event loop for all listeners, so restarting one keeps sessions alive."""
    global _loop, _pool
    with _loop_lock:
        if _loop is None:
            _loop = asyncio.new_event_loop()
            _pool = concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix="textual_repl.exec")
            threading.Thread(target=_loop.run_forever, name="textual_repl.ssh", daemon=True).start()
        return _loop


class ReplServer:
    """An SSH listener. Sessions outlive the listener that accepted them."""

    sessions: ClassVar[set[str]] = set()  # shared by every listener

    def __init__(self, host: str, port: int, host_key_path: str, authorized_keys_path: str):
        self.host = host
        self.port = port
        self.host_key_path = host_key_path
        self.authorized_keys_path = authorized_keys_path
        self._loop = _ssh_loop()
        self._pool = _pool
        self._server = None

    def start(self) -> None:
        ensure_host_key(self.host_key_path)
        future = asyncio.run_coroutine_threadsafe(self._listen(), self._loop)
        future.result(timeout=30)

    def stop(self) -> None:
        """Stop listening (open sessions carry on)."""
        if self._server is not None:
            server, self._server = self._server, None
            self._loop.call_soon_threadsafe(server.close)
            log.info("Stopped listening on %s:%s", self.host, self.port)

    async def _listen(self) -> None:
        self._server = await asyncssh.create_server(
            lambda: _ServerProtocol(self),
            self.host,
            self.port,
            server_host_keys=[self.host_key_path],
            process_factory=self._handle,
            encoding="utf-8",
            line_editor=False,
            allow_scp=False,
            agent_forwarding=False,
            x11_forwarding=False,
            reuse_address=True,
        )
        log.info("Listening for SSH on %s:%s", self.host, self.port)

    async def _handle(self, process: asyncssh.SSHServerProcess) -> None:
        host, port = (process.get_extra_info("peername") or ("?", "?"))[:2]
        peer = f"{host}:{port}"
        label = f"{peer} ({'interactive' if self._is_interactive(process) else 'exec'})"
        self.sessions.add(label)
        try:
            if self._is_interactive(process):
                await self._interactive(process, peer)
            else:
                await self._exec(process)
        except Exception:
            log.exception("SSH session %s failed", peer)
            _safe_exit(process, 255)
        finally:
            self.sessions.discard(label)

    @staticmethod
    def _is_interactive(process) -> bool:
        return process.command is None and process.get_terminal_type() is not None

    # -- interactive ------------------------------------------------------------

    async def _interactive(self, process, peer: str) -> None:
        loop = asyncio.get_running_loop()
        closed = asyncio.Event()
        columns, rows, *_ = process.get_terminal_size()

        def write(text: str) -> None:
            loop.call_soon_threadsafe(_safe_write, process, text.replace("\n", "\r\n"))

        session = shell.InteractiveSession(
            write,
            columns,
            rows,
            process.get_terminal_type(),
            peer,
            on_close=lambda: loop.call_soon_threadsafe(closed.set),
        )
        await loop.run_in_executor(None, session.start)

        reader = asyncio.create_task(self._pump_interactive(process, session))
        await closed.wait()
        reader.cancel()
        _safe_exit(process, 0)

    async def _pump_interactive(self, process, session) -> None:
        try:
            while True:
                try:
                    data = await process.stdin.read(4096)
                except asyncssh.TerminalSizeChanged as exc:
                    session.resize(exc.width, exc.height)
                    continue
                except asyncssh.BreakReceived:
                    session.interrupt()
                    continue
                except asyncssh.SignalReceived as exc:
                    if exc.signal == "INT":
                        session.interrupt()
                    continue
                if not data:
                    break
                session.feed(data)
        except (asyncssh.Error, ConnectionError, BrokenPipeError):
            pass
        finally:
            session.close()

    # -- exec ---------------------------------------------------------------

    async def _exec(self, process) -> None:
        loop = asyncio.get_running_loop()
        code = process.command
        if code is None:
            code = await process.stdin.read()

        translate = process.get_terminal_type() is not None

        def write(text: str) -> None:
            if translate:
                text = text.replace("\n", "\r\n")
            loop.call_soon_threadsafe(_safe_write, process, text)

        stream = CallbackStream(write)
        executor = shell.Executor()
        execution = loop.run_in_executor(self._pool, shell.execute, code, stream, executor)
        watcher = asyncio.create_task(self._watch_exec(process, executor))

        done, _ = await asyncio.wait({execution, watcher}, return_when=asyncio.FIRST_COMPLETED)
        watcher.cancel()

        if execution in done:
            stream.close()
            _safe_exit(process, 0 if execution.result() else 1)
        else:
            # Client went away mid-execution; stop the code.
            executor.interrupt()
            stream.close()

    async def _watch_exec(self, process, executor) -> None:
        """Interrupt on Ctrl-C / SIGINT; return once the client disconnects."""
        try:
            while True:
                try:
                    data = await process.stdin.read(1024)
                except asyncssh.SignalReceived as exc:
                    if exc.signal == "INT":
                        executor.interrupt()
                    continue
                except (asyncssh.TerminalSizeChanged, asyncssh.BreakReceived):
                    continue
                if "\x03" in data:
                    executor.interrupt()
                if not data:
                    break
        except (asyncssh.Error, ConnectionError, BrokenPipeError):
            pass
        await process.channel.wait_closed()
