"""SSH exec requests: `ssh host 'code'` and `ssh host < script`."""

import subprocess
import time
import unittest

from . import support


class ExecTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.h = support.harness()

    def test_expression_value_is_printed_plainly(self):
        result = self.h.run("6 * 7")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "42\n")  # no colours, no Out[n]: prompt

    def test_exceptions_exit_non_zero(self):
        result = self.h.run("1 / 0")
        self.assertEqual(result.returncode, 1)
        self.assertIn("ZeroDivisionError", result.stdout)

    def test_client_vanishing_mid_output_is_harmless(self):
        # Regression: writing to a socket whose client had gone raised
        # SIGPIPE, which killed the host process (i.e. Textual).
        streamer = "while True: print('x' * 65536, flush=True)"
        for _ in range(5):
            client = subprocess.Popen(
                [*self.h.ssh_command(), streamer], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE
            )
            client.stdout.read(10_000)  # output is streaming
            client.kill()  # vanish without closing the channel
            client.wait()
            client.stdout.close()
            time.sleep(0.5)
            self.assertIsNone(self.h.process.poll(), "the harness died")
        self.assertEqual(self.h.eval("'still here'"), "'still here'")

    def test_code_from_stdin(self):
        result = self.h.run(stdin="total = sum(range(5))\nprint('total', total)\n")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "total 10")

    def test_namespace_persists_between_requests(self):
        self.h.eval("persisted_value = 'kept'")
        self.assertEqual(self.h.eval("persisted_value"), "'kept'")

    def test_trailing_semicolon_suppresses_output(self):
        self.assertEqual(self.h.eval("123;"), "")

    def test_top_level_await(self):
        self.assertEqual(self.h.eval("await asyncio.sleep(0, result='awaited')"), "'awaited'")

    def test_shell_escape_output_is_captured(self):
        self.assertEqual(self.h.eval("!echo from-a-shell"), "from-a-shell")

    def test_help_is_available(self):
        self.assertIn("Entry point to Textual", self.h.eval("help(irc)"))

    def test_version_is_set_by_the_build(self):
        self.assertRegex(self.h.eval("textual_repl.__version__"), r"^'\d+\.\d+\.\d+'$")

    @unittest.skipIf(support.SANDBOXED, "the sandboxed harness can't be given startup scripts")
    def test_startup_scripts_run(self):
        self.assertEqual(self.h.eval("STARTUP_SCRIPT_RAN"), "True")


class IrcApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.h = support.harness()

    def test_clients(self):
        self.assertEqual(self.h.eval("[c.name for c in irc.clients]"), "['Libera', 'OFTC']")
        self.assertEqual(self.h.eval("irc['libera.chat'].nick, irc['OFTC'].connected"), "('tester', False)")

    def test_channels_and_members(self):
        self.assertEqual(self.h.eval("[c.name for c in irc['libera'].channels]"), "['#python', '#textual']")
        self.assertEqual(
            self.h.eval("[str(m) for m in irc.channel('libera/#python').members]"), "['@alice', '+bob', 'tester']"
        )
        self.assertEqual(self.h.eval("irc.selected.name"), "'#python'")

    def test_sending(self):
        mark = self.h.mark()
        self.h.eval("irc['libera']['#python'].say('hello\\nworld')")
        self.h.expect(r"^>> PRIVMSG #python :hello$", mark)
        self.h.expect(r"^>> PRIVMSG #python :world$", mark)

    def test_raw_lines_reject_newlines(self):
        result = self.h.run("irc['libera'].send('PRIVMSG #x :a\\r\\nQUIT')")
        self.assertEqual(result.returncode, 1)
        self.assertIn("cannot contain CR, LF", result.stdout)

    def test_print_locally(self):
        mark = self.h.mark()
        self.h.eval("irc['libera'].print('only local', channel='#textual')")
        self.h.expect(r"^\[#textual\] only local$", mark)

    def test_main_thread_is_respected(self):
        # The mocks print "!!" if Textual objects are touched off the main thread.
        self.h.eval("[ (c.name, c.channels, c.users) for c in irc.clients ]")
        self.assertFalse([line for line in self.h.lines if line.startswith("!!")])


class TextualCommandTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.h = support.harness()

    def test_py_command(self):
        mark = self.h.mark()
        self.h.send("/py [c.name for c in irc.clients]")
        self.h.expect(r"^\[#python\] >>> \[c\.name for c in irc\.clients\]$", mark)
        self.h.expect(r"^\[#python\] \['Libera', 'OFTC'\]$", mark)

    def test_pyrepl_command(self):
        mark = self.h.mark()
        self.h.send("/pyrepl")
        self.h.expect(rf"^\[#python\] Python REPL: Listening on 127\.0\.0\.1:{self.h.port}", mark)
