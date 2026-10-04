"""Shared test fixture: a running mock Textual with the plugin loaded.

``make test`` builds tests/harness/Harness.m (which loads PythonREPL.bundle
the way Textual's plugin manager does, with mock IRC objects) and runs this
suite with the bundle's own Python, which already has pexpect.

The harness echoes everything "Textual" would do to stdout:

    >> PRIVMSG #python :hi         sent to the server
    [#python] text                  printed locally in a window
    << DROPPED <line>               server input the plugin swallowed
    -- created window <name>        a window was created

and lines written to its stdin are injected as server input (or ``/py``,
``/pyrepl`` and ``!snapshot <png>`` commands).
"""

from __future__ import annotations

import atexit
import os
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest

SANDBOXED = os.environ.get("HARNESS_SANDBOXED") == "1"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Harness:
    def __init__(self):
        self.harness = os.environ["HARNESS"]
        self.bundle = os.environ["BUNDLE"]
        self.directory = tempfile.mkdtemp(prefix="textual-repl-test-")
        self.key = os.path.join(self.directory, "id_ed25519")
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "test", "-f", self.key], check=True)
        with open(self.key + ".pub") as handle:
            self.public_key = handle.read()
        self.port = free_port()
        self.lines: list[str] = []
        self._changed = threading.Condition()
        self.process = None

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        config = f'{{"host": "127.0.0.1", "port": {self.port}}}'
        env = dict(os.environ)
        if SANDBOXED:
            # A sandboxed process can't be handed files outside its
            # container, so it seeds its own support folder from these.
            support = "-"
            env.update(HARNESS_AUTHORIZED_KEY=self.public_key, HARNESS_CONFIG=config)
        else:
            support = os.path.join(self.directory, "support")
            repl = os.path.join(support, "Python REPL")
            os.makedirs(os.path.join(repl, "startup"))
            with open(os.path.join(repl, "config.json"), "w") as handle:
                handle.write(config)
            with open(os.path.join(repl, "authorized_keys"), "w") as handle:
                handle.write(self.public_key)
            with open(os.path.join(repl, "startup", "00_test.py"), "w") as handle:
                handle.write("STARTUP_SCRIPT_RAN = True\n")

        self.process = subprocess.Popen(
            [self.harness, self.bundle, support],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
        )
        threading.Thread(target=self._read_output, daemon=True).start()
        self.wait_until_listening(self.port)

    def stop(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(5)
            except subprocess.TimeoutExpired:
                self.process.kill()
        shutil.rmtree(self.directory, ignore_errors=True)

    def wait_until_listening(self, port: int, timeout: float = 30.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError("Harness exited:\n" + "\n".join(self.lines))
            try:
                socket.create_connection(("127.0.0.1", port), 0.2).close()
                return
            except OSError:
                time.sleep(0.1)
        raise TimeoutError(f"Nothing listening on {port}:\n" + "\n".join(self.lines))

    def _read_output(self) -> None:
        for line in self.process.stdout:
            with self._changed:
                self.lines.append(line.rstrip("\n"))
                self._changed.notify_all()

    # -- driving it ----------------------------------------------------------

    def ssh_command(self, *extra: str, port: int | None = None) -> list[str]:
        return [
            "ssh",
            "-i",
            self.key,
            "-o",
            "LogLevel=ERROR",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
            "-p",
            str(port or self.port),
            *extra,
            "127.0.0.1",
        ]

    def run(
        self, code: str | None = None, stdin: str | None = None, port: int | None = None, timeout: float = 30.0
    ) -> subprocess.CompletedProcess:
        """Run code with an SSH exec request (or feed it on stdin)."""
        command = self.ssh_command(port=port) + ([code] if code is not None else [])
        return subprocess.run(
            command, input=stdin if stdin is not None else "", capture_output=True, text=True, timeout=timeout
        )

    def eval(self, code: str) -> str:
        """Run code and return stdout, failing on a non-zero exit."""
        result = self.run(code)
        if result.returncode != 0:
            raise AssertionError(f"{code!r} failed ({result.returncode}):\n{result.stdout}{result.stderr}")
        return result.stdout.strip()

    def send(self, line: str) -> None:
        """Inject a server line, or a /py, /pyrepl or !snapshot command."""
        self.process.stdin.write(line + "\n")
        self.process.stdin.flush()

    def mark(self) -> int:
        with self._changed:
            return len(self.lines)

    def settle(self, quiet: float = 0.5, timeout: float = 10.0) -> None:
        """Wait until the harness has printed nothing for ``quiet`` seconds.

        Output from earlier tests (e.g. lines printed into REPL windows) is
        delivered asynchronously; settle first so it isn't mistaken for ours.
        """
        deadline = time.monotonic() + timeout
        count = self.mark()
        while time.monotonic() < deadline:
            time.sleep(quiet)
            latest = self.mark()
            if latest == count:
                return
            count = latest

    def expect(self, pattern: str, since: int = 0, timeout: float = 10.0) -> re.Match:
        """Wait for a harness output line matching ``pattern`` (after ``since``)."""
        regex = re.compile(pattern)
        deadline = time.monotonic() + timeout
        with self._changed:
            while True:
                for line in self.lines[since:]:
                    match = regex.search(line)
                    if match:
                        return match
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError(f"No output matching {pattern!r}. Got:\n" + "\n".join(self.lines[since:]))
                self._changed.wait(remaining)

    def output_since(self, since: int, settle: float = 0.5) -> list[str]:
        time.sleep(settle)
        with self._changed:
            return self.lines[since:]


_harness: Harness | None = None


def harness() -> Harness:
    """The shared harness, started on first use."""
    global _harness
    if _harness is None:
        if "HARNESS" not in os.environ:
            raise unittest.SkipTest("Run the tests with `make test`")
        _harness = Harness()
        atexit.register(_harness.stop)
        _harness.start()
    return _harness
