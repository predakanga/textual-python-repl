"""Bootstrap, called by the TPI_PythonREPL shim right after Python starts.

``start()`` runs on Textual's plugin queue with the GIL held, so it only
does the minimum and hands everything else to a background thread.
"""

from __future__ import annotations

import asyncio
import json
import logging
import logging.handlers
import os
import signal
import sys
import threading

import objc
from Foundation import NSObject

from . import mainthread
from .output import BufferStream, LogStream, Router

log = logging.getLogger("textual_repl")

DEFAULT_CONFIG = {
    "enabled": True,
    "host": "127.0.0.1",
    "port": 2323,
    "request_output": "window",
}

REQUEST_OUTPUT_MODES = ("window", "hidden", "textual")

MAX_COMMAND_OUTPUT_LINES = 40

SUPPORT_DIR: str = ""
CONFIG: dict = {}
IRC = None
SERVER = None
LOOP: asyncio.AbstractEventLoop | None = None
READY = threading.Event()
BOOT_ERROR: str | None = None
SERVER_ERROR: str | None = None
STARTUP_FAILURES: list[str] = []

_shim = None
_delegate = None
_server_lock = threading.Lock()


def _support_dir() -> str:
    try:
        base = objc.lookUpClass("TPCPathInfo").groupContainerApplicationSupport()
        if base:
            return os.path.join(str(base), "Python REPL")
    except Exception:
        pass
    return os.path.expanduser("~/Library/Application Support/Textual Python REPL")


def _setup_logging(directory: str) -> None:
    handler = logging.handlers.RotatingFileHandler(
        os.path.join(directory, "python-repl.log"), maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s [%(threadName)s] %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    logging.getLogger("asyncssh").setLevel(logging.WARNING)


def _load_config(directory: str) -> dict:
    path = os.path.join(directory, "config.json")
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(DEFAULT_CONFIG, handle, indent=4)
            handle.write("\n")
    try:
        with open(path, encoding="utf-8") as handle:
            return {**DEFAULT_CONFIG, **json.load(handle)}
    except Exception:
        log.exception("Could not read %s; using defaults", path)
        return dict(DEFAULT_CONFIG)


def _save_config() -> None:
    path = os.path.join(SUPPORT_DIR, "config.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(CONFIG, handle, indent=4)
        handle.write("\n")


def _authorized_keys_path() -> str:
    return os.path.join(SUPPORT_DIR, "authorized_keys")


def _ensure_authorized_keys(path: str) -> None:
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("# Public keys allowed to connect to Textual's Python REPL, one per line.\n")
        os.chmod(path, 0o600)


class PluginDelegate(NSObject):
    """Receives the callbacks TPI_PythonREPL forwards (see TPI_PythonREPL.m).

    Nothing may raise back into Objective-C, so every method catches
    everything.
    """

    def interceptServerInput_client_(self, message, client):
        try:
            return IRC._bus.intercept(message, client)
        except Exception:
            log.exception("interceptServerInput failed")
            return message

    def userCommand_message_client_(self, command, message, client):
        try:
            threading.Thread(
                target=_run_user_command,
                args=(str(command), str(message), client),
                name="textual_repl.command",
                daemon=True,
            ).start()
        except Exception:
            log.exception("userCommand failed")
        return None

    def shutdown(self):
        try:
            if SERVER is not None:
                SERVER.stop()
        except Exception:
            log.exception("shutdown failed")
        return None

    # -- preferences pane (called on the main thread) ------------------------

    def preferences(self):
        try:
            return _preferences()
        except Exception:
            log.exception("preferences failed")
            return None

    def applyPreferences_(self, values):
        try:
            return _apply_preferences({str(k): values[k] for k in values})
        except Exception as exc:
            log.exception("applyPreferences failed")
            return f"{type(exc).__name__}: {exc}"

    def status(self):
        try:
            return _status()
        except Exception:
            log.exception("status failed")
            return None


def start(bundle_path: str) -> None:
    global SUPPORT_DIR, CONFIG, _shim, _delegate

    SUPPORT_DIR = _support_dir()
    os.makedirs(SUPPORT_DIR, exist_ok=True)
    _setup_logging(SUPPORT_DIR)
    log.info("Starting Python %s from %s", sys.version.split()[0], bundle_path)

    CONFIG = _load_config(SUPPORT_DIR)

    sys.stdout = Router(LogStream(logging.getLogger("textual_repl.stdout"), logging.INFO))
    sys.stderr = Router(LogStream(logging.getLogger("textual_repl.stderr"), logging.WARNING))

    _shim = objc.lookUpClass("TPI_PythonREPL")
    _delegate = PluginDelegate.alloc().init()

    threading.Thread(target=_boot, name="textual_repl.boot", daemon=True).start()


def _configure_server() -> None:
    """(Re)start or stop the SSH listener to match CONFIG."""
    global SERVER, SERVER_ERROR
    from . import sshserver

    with _server_lock:
        if SERVER is not None:
            SERVER.stop()
            SERVER = None
        SERVER_ERROR = None
        if not CONFIG.get("enabled", True):
            log.info("SSH server disabled")
            return
        server = sshserver.ReplServer(
            str(CONFIG["host"]),
            int(CONFIG["port"]),
            os.path.join(SUPPORT_DIR, "ssh_host_ed25519_key"),
            _authorized_keys_path(),
        )
        try:
            server.start()
        except Exception as exc:
            SERVER_ERROR = f"Couldn't listen on {CONFIG['host']}:{CONFIG['port']}: {exc}"
            log.exception("SSH server failed to start")
            return
        SERVER = server


def _boot() -> None:
    global IRC, LOOP, BOOT_ERROR

    # Writing to a socket or pipe whose other end has gone raises SIGPIPE,
    # which kills the process by default. Python normally ignores it, but we
    # leave Textual's signal dispositions alone, so instead block it in our
    # threads (all of which descend from this one): those writes then just
    # fail with EPIPE.
    signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGPIPE})

    try:
        from . import irc as irc_module
        from . import shell

        LOOP = asyncio.new_event_loop()
        threading.Thread(target=LOOP.run_forever, name="textual_repl.irc", daemon=True).start()

        IRC = irc_module.create(LOOP, lambda flag: _shim.setInterceptsServerInput_(bool(flag)))
        irc_module.request_output = CONFIG.get("request_output", "window")
        _shim.setPythonDelegate_(_delegate)

        startup_dir = os.path.join(SUPPORT_DIR, "startup")
        os.makedirs(startup_dir, exist_ok=True)

        import IPython

        banner = (
            f"Textual Python REPL — Python {sys.version.split()[0]}, IPython {IPython.__version__}\n"
            "  irc               entry point: irc.clients, irc['name'], irc.selected   (help(irc))\n"
            "  @irc.on(...)      register handlers; also irc.filter, irc.wait_for, irc.spawn\n"
            "  .objc             every wrapper exposes the raw Textual object via PyObjC\n"
            f"  startup scripts   {startup_dir}\n"
        )
        shell.setup(LOOP, os.path.join(SUPPORT_DIR, "ipython"), banner)

        import textual_repl

        shell.NAMESPACE.update(
            irc=IRC,
            objc=objc,
            asyncio=asyncio,
            on_main=mainthread.call,
            textual_repl=textual_repl,
            Client=irc_module.Client,
            Channel=irc_module.Channel,
            Event=irc_module.Event,
        )

        STARTUP_FAILURES[:] = shell.run_startup_scripts(startup_dir)

        _ensure_authorized_keys(_authorized_keys_path())
        _configure_server()
        log.info("Ready")
    except BaseException as exc:
        BOOT_ERROR = f"{type(exc).__name__}: {exc}"
        log.exception("Boot failed")
    finally:
        READY.set()


# --------------------------------------------------------------------------
# /py and /pyrepl in Textual's input field
# --------------------------------------------------------------------------


def _ssh_command() -> str | None:
    if SERVER is None:
        return None
    host = "127.0.0.1" if SERVER.host in ("0.0.0.0", "::", "") else SERVER.host
    return f"ssh -p {SERVER.port} {host}"


def _server_summary() -> tuple[str, bool]:
    """One line describing the SSH server, and whether all is well."""
    if BOOT_ERROR:
        return f"Python REPL failed to start: {BOOT_ERROR}", False
    if not READY.is_set():
        return "Starting…", True
    if SERVER_ERROR:
        return SERVER_ERROR, False
    if SERVER is None:
        return "SSH server is turned off (/py and REPL windows still work)", True
    from .sshserver import ReplServer

    sessions = len(ReplServer.sessions)
    return f"Listening on {SERVER.host}:{SERVER.port} — {sessions} active session{'s' * (sessions != 1)}", True


def _status() -> dict:
    text, ok = _server_summary()
    return {"text": text, "ok": ok, "command": _ssh_command() or ""}


def _preferences() -> dict:
    try:
        with open(_authorized_keys_path(), encoding="utf-8") as handle:
            keys = handle.read()
    except OSError:
        keys = ""
    return {
        "enabled": bool(CONFIG.get("enabled", True)),
        "host": str(CONFIG.get("host", "127.0.0.1")),
        "port": int(CONFIG.get("port", 2323)),
        "request_output": str(CONFIG.get("request_output", "window")),
        "authorized_keys": keys,
        "support_dir": SUPPORT_DIR,
    }


def _apply_preferences(values: dict) -> str | None:
    """Validate and save values from the preferences pane; returns an error or None."""
    import asyncssh

    try:
        port = int(values["port"])
    except (TypeError, ValueError):
        return "The port must be a number."
    if not 1 <= port <= 65535:
        return "The port must be between 1 and 65535."
    host = str(values["host"]).strip()
    if not host:
        return "Enter an address to listen on."
    request_output = str(values["request_output"])
    if request_output not in REQUEST_OUTPUT_MODES:
        return f"Unknown request output mode {request_output!r}."

    keys = str(values["authorized_keys"]).replace("\r\n", "\n")
    if keys and not keys.endswith("\n"):
        keys += "\n"
    # asyncssh skips bad lines if any line is good, so check each one.
    for number, line in enumerate(keys.splitlines(), start=1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        try:
            asyncssh.import_authorized_keys(line + "\n")
        except Exception:
            return f"Authorized keys line {number} isn't a valid public key."

    with open(_authorized_keys_path(), "w", encoding="utf-8") as handle:
        handle.write(keys)
    os.chmod(_authorized_keys_path(), 0o600)

    restart = (
        bool(values["enabled"]) != bool(CONFIG.get("enabled", True))
        or host != CONFIG.get("host")
        or port != CONFIG.get("port")
        or SERVER_ERROR is not None
    )
    CONFIG.update(enabled=bool(values["enabled"]), host=host, port=port, request_output=request_output)
    _save_config()

    from . import irc as irc_module

    irc_module.request_output = request_output

    if restart and READY.is_set() and not BOOT_ERROR:
        threading.Thread(target=_configure_server, name="textual_repl.restart", daemon=True).start()
    return None


def _status_lines() -> list[str]:
    summary, _ = _server_summary()
    if BOOT_ERROR:
        return [summary, f"See {os.path.join(SUPPORT_DIR, 'python-repl.log')}"]
    from .sshserver import ReplServer

    lines = [f"Python REPL: {summary}"]
    if _ssh_command():
        lines.append(f"Connect with: {_ssh_command()}")
    lines += [
        f"Support folder: {SUPPORT_DIR}",
        f"Active sessions: {', '.join(sorted(ReplServer.sessions)) or 'none'}",
    ]
    handlers = IRC.handlers if IRC is not None else []
    lines.append(f"Handlers: {', '.join(h.name for h in handlers) or 'none'}")
    if IRC is not None and IRC.tasks:
        lines.append(f"Tasks: {', '.join(IRC.tasks)}")
    for failure in STARTUP_FAILURES:
        lines.append(f"Startup script failed — {failure} (details in python-repl.log)")
    return lines


def _run_user_command(command: str, message: str, client) -> None:
    try:
        READY.wait(30)
        if command.lower() == "pyrepl" or BOOT_ERROR:
            _print_to_textual(client, _status_lines())
        elif not message.strip():
            _print_to_textual(
                client, ["Usage: /py <python code>   — runs in the REPL namespace, e.g. /py irc.selected"]
            )
        else:
            _run_code(message, client)
    except Exception:
        log.exception("/%s failed", command)


def _run_code(code: str, client) -> None:
    """Run code for /py, printing the output in the active window."""
    try:
        READY.wait(30)
        if BOOT_ERROR:
            _print_to_textual(client, _status_lines())
            return
        from . import shell

        stream = BufferStream()
        shell.execute(code, stream)
        output = stream.getvalue().rstrip("\n").splitlines()
        if len(output) > MAX_COMMAND_OUTPUT_LINES:
            hidden = len(output) - MAX_COMMAND_OUTPUT_LINES
            output = [*output[:MAX_COMMAND_OUTPUT_LINES], f"… {hidden} more lines (use the SSH REPL for long output)"]
        _print_to_textual(client, [f">>> {code}", *output])
    except Exception:
        log.exception("Running code from Textual failed")


def _print_to_textual(client, lines: list[str]) -> None:
    def run():
        window = objc.lookUpClass("NSObject").masterController().mainWindow()
        target = window.selectedChannel() if window.selectedClient() == client else None
        for line in lines:
            if target is not None:
                client.printDebugInformation_inChannel_(line, target)
            else:
                client.printDebugInformationToConsole_(line)

    mainthread.call(run)
