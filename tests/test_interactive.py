"""Interactive IPython sessions over SSH (driven through a pty)."""

import re
import subprocess
import unittest

import pexpect

from . import support

COLOUR = r"(?:\x1b\[[0-9;]*m)*"
PROMPT = rf"In \[{COLOUR}(\d+){COLOUR}\]: "


def prompt(number):
    return rf"In \[{COLOUR}{number}{COLOUR}\]: "


class InteractiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.h = support.harness()

    def open_session(self):
        command = self.h.ssh_command("-tt")
        session = pexpect.spawn(command[0], command[1:], encoding="utf-8", dimensions=(40, 160), timeout=15)
        self.addCleanup(session.close, force=True)
        session.expect("Textual Python REPL")
        session.expect(r"REPL window +replies to whois\(\) etc\. appear in '(Python REPL \d+)'")
        window = session.match.group(1)
        session.expect(PROMPT)
        session.cell = int(session.match.group(1))
        return session, window

    def run_cell(self, session, code):
        """Run a cell and return its (uncoloured) output.

        Waits for the *next* prompt number: prompt_toolkit redraws the
        current prompt while the line is typed.
        """
        session.send(code + "\r")
        session.expect(prompt(session.cell + 1))
        session.cell += 1
        return re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", session.before).replace("\r", "")

    def test_session(self):
        session, _ = self.open_session()
        self.assertIn("42", self.run_cell(session, "21 * 2"))
        self.assertIn("'awaited'", self.run_cell(session, "await asyncio.sleep(0.05, result='awaited')"))
        self.assertIn("<Channel #python", self.run_cell(session, "irc.selected"))
        self.run_cell(session, "from_session = 'shared'")
        self.assertEqual(self.h.eval("from_session"), "'shared'")
        session.send("exit\r")
        session.expect(pexpect.EOF)

    def test_namespace_survives_session_exit(self):
        session, _ = self.open_session()
        self.run_cell(session, "survivor = 1")
        session.send("exit\r")
        session.expect(pexpect.EOF)
        self.assertEqual(self.h.eval("survivor, 'irc' in globals()"), "(1, True)")

    def test_ctrl_c_interrupts_running_code(self):
        session, _ = self.open_session()
        session.send("while True: pass\r\r")
        session.expect(r"while")
        session.send("\x03")
        session.expect("KeyboardInterrupt")
        session.expect(prompt(session.cell + 1))
        session.cell += 1
        self.assertIn("still alive", self.run_cell(session, "'still alive'"))

    def test_background_output_appears_above_the_prompt(self):
        session, _ = self.open_session()
        self.run_cell(session, "async def later(): await asyncio.sleep(0.3); print('from', 'the background')")
        self.run_cell(session, "irc.spawn(later());")
        session.expect(r"from the background\r\n")

    def test_completion_and_help(self):
        session, _ = self.open_session()
        session.send("irc.cli\t")
        session.expect("clients")
        session.send("\x15")  # clear the line
        session.send("irc?\r")
        session.expect("Entry point to Textual")

    def test_each_session_gets_its_own_window(self):
        first, first_window = self.open_session()
        second, second_window = self.open_session()
        self.assertNotEqual(first_window, second_window)
        mark = self.h.mark()
        self.run_cell(second, "await irc['libera'].whois('carol');")
        self.h.expect(rf"^\[{re.escape(second_window)}\] carol is user@example\.org", mark)
        for session in (first, second):
            session.send("exit\r")
            session.expect(pexpect.EOF)

    def test_concurrent_exec_while_session_is_idle(self):
        session, _ = self.open_session()
        result = subprocess.run([*self.h.ssh_command(), "1 + 1"], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.stdout.strip(), "2")
        self.assertIn("ok", self.run_cell(session, "'ok'"))
