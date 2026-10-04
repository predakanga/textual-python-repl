"""IPython integration: one shared namespace, many front ends.

Every front end (SSH sessions, SSH exec requests, ``/py`` in Textual)
executes in the same namespace, so state defined by one is visible to the
others. IPython keeps global state (``sys.displayhook``, builtins) while a
cell runs, so execution is serialised by :data:`EXEC_LOCK`.

* Interactive SSH sessions get their own ``TerminalInteractiveShell``
  driven by prompt_toolkit over the SSH channel, in a dedicated thread.
* Exec requests and ``/py`` share a plain ``InteractiveShell`` with no
  colours and no ``Out[n]:`` prompts, which is easier for programs (and AI
  agents) to consume.

Cells that use top-level ``await`` run on the IRC event loop, so tasks they
create keep running after the cell finishes.
"""

from __future__ import annotations

import atexit
import contextlib
import contextvars
import ctypes
import io
import logging
import os
import subprocess
import sys
import threading

from IPython.core.displayhook import DisplayHook
from IPython.core.interactiveshell import InteractiveShell
from IPython.terminal.interactiveshell import TerminalInteractiveShell
from prompt_toolkit.application import create_app_session, run_in_terminal
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output.vt100 import Vt100_Output
from traitlets import Type
from traitlets.config import Config

from . import windows
from .output import current_stream

log = logging.getLogger(__name__)

NAMESPACE: dict = {"__name__": "__main__"}
EXEC_LOCK = threading.RLock()
BANNER = ""

current_shell: contextvars.ContextVar = contextvars.ContextVar("textual_repl_shell", default=None)

_loop = None
_exec_shell: InteractiveShell | None = None
_original_get_ipython = None


def get_ipython():
    """Context-aware replacement for ``IPython.get_ipython()``.

    IPython assumes one shell per process; here each session has its own,
    and IPython's key binding filters (among others) must see that one.
    """
    shell = current_shell.get()
    if shell is not None:
        return shell
    return _original_get_ipython()


def _install_get_ipython() -> None:
    global _original_get_ipython
    import IPython.core.getipython

    _original_get_ipython = IPython.core.getipython.get_ipython
    for module in list(sys.modules.values()):
        try:
            if getattr(module, "get_ipython", None) is _original_get_ipython:
                module.get_ipython = get_ipython
        except Exception:
            pass


# --------------------------------------------------------------------------
# Execution bookkeeping
# --------------------------------------------------------------------------


def _set_async_exc(thread_id: int, exc_type) -> None:
    ctypes.pythonapi.PyThreadState_SetAsyncExc(
        ctypes.c_ulong(thread_id), ctypes.py_object(exc_type) if exc_type is not None else None
    )


class Executor:
    """Tracks the thread running a front end's code so it can be interrupted."""

    def __init__(self):
        self._lock = threading.Lock()
        self._thread_id: int | None = None
        self._depth = 0

    @contextlib.contextmanager
    def executing(self):
        me = threading.get_ident()
        with self._lock:
            self._depth += 1
            self._thread_id = me
        try:
            if not EXEC_LOCK.acquire(timeout=0.5):
                print("[waiting for code running in another session to finish]", file=sys.stderr, flush=True)
                while not EXEC_LOCK.acquire(timeout=0.1):
                    pass
            try:
                yield
            finally:
                EXEC_LOCK.release()
        finally:
            with self._lock:
                self._depth -= 1
                if self._depth == 0:
                    self._thread_id = None
                    # Discard an interrupt that arrived too late to matter.
                    _set_async_exc(me, None)

    def interrupt(self) -> bool:
        """Raise KeyboardInterrupt in the executing thread, if any."""
        with self._lock:
            if self._thread_id is None:
                return False
            _set_async_exc(self._thread_id, KeyboardInterrupt)
            return True


def _loop_runner(coro):
    """IPython ``loop_runner``: run top-level-await cells on the IRC loop."""
    context = contextvars.copy_context()
    finished = threading.Event()
    holder = {}

    def start():
        task = _loop.create_task(coro, context=context)
        holder["task"] = task
        task.add_done_callback(lambda _: finished.set())

    _loop.call_soon_threadsafe(start)

    while True:
        try:
            if finished.wait(0.1):
                break
        except KeyboardInterrupt:
            task = holder.get("task")
            if task is not None:
                _loop.call_soon_threadsafe(task.cancel)

    task = holder["task"]
    if task.cancelled():
        raise KeyboardInterrupt
    return task.result()


def _system(shell, cmd: str) -> None:
    """``!command`` support: capture output instead of writing to Textual's fd 1."""
    proc = subprocess.run(cmd, shell=True, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    shell.user_ns["_exit_code"] = proc.returncode


def _show_in_pager(shell, data, start=0, screen_lines=0, pager_cmd=None):
    """``obj?`` support: print instead of spawning a pager on Textual's fd 1."""
    if isinstance(data, dict):
        data = data.get("text/plain", "")
    print("\n".join(str(data).splitlines()[start:]))


# --------------------------------------------------------------------------
# Exec shell (SSH exec requests, /py)
# --------------------------------------------------------------------------


class PlainDisplayHook(DisplayHook):
    """Show results without the ``Out[n]:`` prefix."""

    def write_output_prompt(self):
        pass

    def quiet(self):
        # IPython looks for a trailing ';' in the input history, which exec
        # requests don't record; check the cell being run instead.
        return self.semicolon_at_end_of_expression(getattr(self.shell, "current_cell", ""))


class ExecShell(InteractiveShell):
    displayhook_class = Type(PlainDisplayHook)

    def system(self, cmd):
        _system(self, cmd)

    system_raw = system


def _base_config() -> Config:
    config = Config()
    config.HistoryManager.enabled = True
    config.InteractiveShell.enable_tip = False
    return config


def exec_shell() -> InteractiveShell:
    global _exec_shell
    if _exec_shell is None:
        config = _base_config()
        config.InteractiveShell.colors = "nocolor"
        config.InteractiveShell.xmode = "Context"
        config.HistoryManager.enabled = False
        _exec_shell = ExecShell.instance(config=config, user_ns=NAMESPACE)
        _exec_shell.loop_runner = _loop_runner
        NAMESPACE["get_ipython"] = get_ipython
        _exec_shell.set_hook("show_in_pager", _show_in_pager)
    return _exec_shell


def execute(code: str, stream: io.TextIOBase, executor: Executor | None = None) -> bool:
    """Run ``code`` in the shared namespace, writing output to ``stream``.

    Returns True on success. Safe to call from any thread except the IRC
    event loop (top-level ``await`` would deadlock there).
    """
    return contextvars.copy_context().run(_execute, code, stream, executor or Executor())


def _execute(code: str, stream: io.TextIOBase, executor: Executor) -> bool:
    current_stream.set(stream)
    shell = exec_shell()
    current_shell.set(shell)
    try:
        with executor.executing():
            shell.current_cell = code
            result = shell.run_cell(code, store_history=False)
        return bool(result.success)
    except KeyboardInterrupt:
        print("KeyboardInterrupt", file=sys.stderr)
        return False
    finally:
        sys.stdout.flush()
        sys.stderr.flush()


# --------------------------------------------------------------------------
# Interactive sessions
# --------------------------------------------------------------------------


class SessionShell(TerminalInteractiveShell):
    """A terminal shell bound to one SSH session."""

    executor: Executor

    def prompt_for_code(self):
        # IPython wraps this in prompt_toolkit's patch_stdout(), which swaps
        # sys.stdout process-wide. With several sessions that would steal
        # their output, so SessionStream handles printing above the prompt.
        default = self.rl_next_input or ""
        self.rl_next_input = None
        return self.pt_app.prompt(default=default, inputhook=self._inputhook, **self._extra_prompt_options())

    def run_cell(self, raw_cell, *args, **kwargs):
        try:
            with self.executor.executing():
                return super().run_cell(raw_cell, *args, **kwargs)
        except KeyboardInterrupt:
            print("KeyboardInterrupt", file=sys.stderr)
            return None

    def system(self, cmd):
        _system(self, cmd)

    system_raw = system

    def enable_gui(self, gui=None):
        if gui:
            raise RuntimeError("GUI event loop integration isn't available inside Textual")

    def _atexit_once(self):
        # mainloop() calls this on exit and IPython's version reset()s the
        # user namespace, which every other session shares. Just close
        # this session's history.
        if getattr(self, "_atexit_once_called", False):
            return
        self._atexit_once_called = True
        atexit.unregister(self.atexit_operations)
        if self.history_manager is not None:
            self.history_manager.end_session()
            self.history_manager.close()
            self.history_manager = None


class _ChannelFile:
    """File-like object prompt_toolkit's Vt100_Output writes to."""

    def __init__(self, session: InteractiveSession):
        self._session = session

    def write(self, data: str) -> int:
        self._session.write_raw(data)
        return len(data)

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return True

    @property
    def encoding(self) -> str:
        return "utf-8"


class SessionStream(io.TextIOBase):
    """sys.stdout/sys.stderr target for one interactive session.

    Writes go straight to the channel while code runs. While the prompt is
    up, complete lines are printed above it with ``run_in_terminal``;
    partial lines wait for their newline (or an explicit flush).
    """

    def __init__(self, session: InteractiveSession):
        self._session = session
        self._lock = threading.Lock()
        self._pending = ""
        self._scheduled_on = None  # loop a flush is already queued on

    def writable(self) -> bool:
        return True

    def isatty(self) -> bool:
        return True

    @property
    def encoding(self) -> str:
        return "utf-8"

    def write(self, text: str) -> int:
        if self.closed:
            raise ValueError("session closed")
        if text:
            with self._lock:
                self._pending += text
            self._schedule(partial=False)
        return len(text)

    def flush(self) -> None:
        if not self.closed:
            self._schedule(partial=True)

    def close(self) -> None:
        if not self.closed:
            self._drain()
        super().close()

    def _schedule(self, partial: bool) -> None:
        app = self._session.running_app()
        if app is None:
            self._drain()
            return
        with self._lock:
            if self._scheduled_on is app.loop and not partial:
                return
            self._scheduled_on = app.loop
        try:
            app.loop.call_soon_threadsafe(self._flush_above_prompt, partial, context=self._session.context.copy())
        except RuntimeError:  # the prompt's loop has already closed
            self._drain()

    def _take(self, partial: bool) -> str:
        with self._lock:
            self._scheduled_on = None
            if partial or self._session.running_app() is None:
                text, self._pending = self._pending, ""
            else:
                cut = self._pending.rfind("\n") + 1
                text, self._pending = self._pending[:cut], self._pending[cut:]
        return text

    def _drain(self) -> None:
        text = self._take(partial=True)
        if text:
            self._session.write_raw(text)

    def _flush_above_prompt(self, partial: bool) -> None:
        text = self._take(partial)
        if not text:
            return
        if not text.endswith("\n"):
            text += "\n"
        run_in_terminal(lambda: self._session.write_raw(text), in_executor=False)


class InteractiveSession:
    """Runs a SessionShell in its own thread, fed by an SSH channel.

    ``write`` must be callable from any thread and deliver text to the
    client (with newline translation already applied by the caller).
    """

    def __init__(self, write, columns: int, rows: int, term: str | None, peer: str, on_close):
        self._write = write
        self._size = Size(rows=rows or 24, columns=columns or 80)
        self._term = term or "xterm"
        self.peer = peer
        self._on_close = on_close
        self._ready = threading.Event()
        self._pipe = None
        self._app_session = None
        self.context: contextvars.Context | None = None
        self.shell: SessionShell | None = None
        self.executor = Executor()
        self.closed = False
        self.thread = threading.Thread(target=self._run, name=f"textual_repl.session[{peer}]", daemon=True)

    def start(self) -> None:
        self.thread.start()
        self._ready.wait(10)

    # -- called from the SSH side -------------------------------------------

    def feed(self, data: str) -> None:
        if "\x03" in data and self.executor.interrupt():
            data = data.replace("\x03", "")
        if data and self._pipe is not None and not self.closed:
            self._pipe.send_text(data)

    def interrupt(self) -> None:
        self.executor.interrupt()

    def resize(self, columns: int, rows: int) -> None:
        self._size = Size(rows=rows, columns=columns)
        app = self.running_app()
        if app is not None:
            app.loop.call_soon_threadsafe(app._on_resize)

    def close(self) -> None:
        """The client went away: stop running code and end the shell."""
        if self.closed:
            return
        self.closed = True
        self.executor.interrupt()
        if self.shell is not None:
            self.shell.keep_running = False
        if self._pipe is not None:
            self._pipe.close()

    # -- helpers ----------------------------------------------------------------

    def write_raw(self, text: str) -> None:
        self._write(text)

    def running_app(self):
        session = self._app_session
        app = session.app if session is not None else None
        if app is not None and app.is_running and app.loop is not None:
            return app
        return None

    # -- session thread ---------------------------------------------------------

    def _run(self) -> None:
        stream = None
        window_name, release_window = windows.allocate_session_window()
        windows.current_window.set(window_name)
        try:
            with create_pipe_input() as pipe:
                self._pipe = pipe
                output = Vt100_Output(_ChannelFile(self), lambda: self._size, term=self._term, enable_cpr=False)
                with create_app_session(input=pipe, output=output) as app_session:
                    self._app_session = app_session
                    stream = SessionStream(self)
                    current_stream.set(stream)

                    config = _base_config()
                    config.TerminalInteractiveShell.simple_prompt = False
                    config.TerminalInteractiveShell.confirm_exit = False
                    config.TerminalInteractiveShell.term_title = False
                    from . import irc as irc_module

                    config.TerminalInteractiveShell.banner1 = BANNER + (
                        f"  REPL window       replies to whois() etc. appear in '{window_name}'\n"
                        if irc_module.request_output == "window"
                        else ""
                    )

                    self.shell = shell = SessionShell(config=config, user_ns=NAMESPACE)
                    NAMESPACE["get_ipython"] = get_ipython
                    current_shell.set(shell)
                    self.context = contextvars.copy_context()
                    shell.executor = self.executor
                    shell.loop_runner = _loop_runner
                    shell.set_hook("show_in_pager", _show_in_pager)
                    self._ready.set()

                    try:
                        shell.show_banner()
                        if not self.closed:
                            shell.mainloop()
                    finally:
                        shell._atexit_once()
        except Exception:
            log.exception("Interactive session for %s failed", self.peer)
            try:
                import traceback

                self.write_raw("\r\nInternal error:\r\n" + traceback.format_exc().replace("\n", "\r\n"))
            except Exception:
                pass
        finally:
            release_window()
            if stream is not None:
                stream.close()
            self.closed = True
            self._ready.set()
            self._on_close()


# --------------------------------------------------------------------------
# Setup
# --------------------------------------------------------------------------


def setup(loop, ipython_dir: str, banner: str) -> None:
    global _loop, BANNER
    _loop = loop
    BANNER = banner
    os.environ["IPYTHONDIR"] = ipython_dir
    os.environ.setdefault("PAGER", "cat")
    os.environ.setdefault("TERM", "xterm-256color")
    _install_get_ipython()
    exec_shell()


def run_startup_scripts(directory: str) -> list[str]:
    """Execute ``*.py`` files in ``directory`` (sorted) in the shared namespace.

    Returns a description of each script that failed.
    """
    failures = []
    if not os.path.isdir(directory):
        return failures
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".py") or name.startswith((".", "_")):
            continue
        path = os.path.join(directory, name)
        try:
            with open(path, encoding="utf-8") as handle:
                code = compile(handle.read(), path, "exec")
            with EXEC_LOCK:
                exec(code, NAMESPACE)
            log.info("Ran startup script %s", path)
        except BaseException as exc:
            log.exception("Startup script %s failed", path)
            failures.append(f"{name}: {type(exc).__name__}: {exc}")
    return failures
