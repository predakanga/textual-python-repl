"""The preferences backend and pane."""

import os
import struct
import tempfile
import unittest

from . import support


class PreferencesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.h = support.harness()

    def apply(self, **changes):
        assignments = ", ".join(f"{key}={value!r}" for key, value in changes.items())
        return self.h.eval(
            f"import textual_repl.plugin as p; p._apply_preferences({{**p._preferences(), **dict({assignments})}})"
        )

    def test_validation(self):
        self.assertIn("between 1 and 65535", self.apply(port=70000))
        self.assertIn("Enter an address", self.apply(host=" "))
        self.assertIn(
            "line 2 isn't a valid public key", self.apply(authorized_keys=self.h.public_key + "ssh-ed25519 nope\n")
        )

    def test_changing_the_port_restarts_the_listener(self):
        old_port, new_port = self.h.port, support.free_port()
        self.assertEqual(self.apply(port=new_port), "")  # None prints nothing
        self.h.wait_until_listening(new_port)
        try:
            self.assertEqual(self.h.run("'moved'", port=new_port).stdout.strip(), "'moved'")
            self.assertNotEqual(self.h.run("1", port=old_port).returncode, 0)
        finally:
            restore = (
                f"import textual_repl.plugin as p; p._apply_preferences({{**p._preferences(), 'port': {old_port}}})"
            )
            self.assertEqual(
                self.h.run(restore, port=new_port).returncode,
                0,
            )
            self.h.wait_until_listening(old_port)

    def test_delegate_accepts_objective_c_values(self):
        # The pane hands over an NSDictionary of NSNumber/NSString.
        output = self.h.eval(
            "from Foundation import NSDictionary\n"
            "import textual_repl.plugin as p\n"
            "values = NSDictionary.dictionaryWithDictionary_({**p._preferences(), 'request_output': 'hidden'})\n"
            "error = p._delegate.applyPreferences_(values)\n"
            "result = (error, textual_repl.irc.request_output)\n"
            "p._apply_preferences({**p._preferences(), 'request_output': 'window'})\n"
            "result"
        )
        self.assertEqual(output, "(None, 'hidden')")

    def test_status(self):
        self.assertEqual(self.h.eval("textual_repl.plugin._delegate.status()['ok']"), "True")


@unittest.skipIf(support.SANDBOXED, "a sandboxed harness can't write snapshots outside its container")
class PaneTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.h = support.harness()

    def snapshot(self, name):
        path = os.path.join(tempfile.mkdtemp(), name)
        mark = self.h.mark()
        self.h.send(f"!snapshot {path}")
        self.h.expect(r"^-- snapshot .* menu item 'Python REPL'", mark, timeout=15)
        with open(path, "rb") as handle:
            header = handle.read(24)
        self.assertEqual(header[:8], b"\x89PNG\r\n\x1a\n")
        return struct.unpack(">II", header[16:24])

    def test_pane_renders_in_light_and_dark_mode(self):
        for name in ("pane-light.png", "pane-dark.png"):
            width, height = self.snapshot(name)
            self.assertGreaterEqual(width, 670)
            self.assertGreaterEqual(height, 470)
