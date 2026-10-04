"""Replies to REPL requests go to REPL windows instead of the active window."""

import unittest

from . import support


class RequestOutputTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.h = support.harness()

    def setUp(self):
        self.h.settle()

    def tearDown(self):
        self.h.eval("textual_repl.irc.request_output = 'window'")

    def test_whois_goes_to_the_shared_window(self):
        mark = self.h.mark()
        self.h.eval("await irc['libera'].whois('bob');")
        self.h.expect(r"^\[Python REPL\] » WHOIS bob$", mark)
        self.h.expect(r"^\[Python REPL\] bob is user@example\.org \(Real Name\)$", mark)
        self.h.expect(r"^<< DROPPED .* 318 tester bob ", mark)
        self.assertFalse([line for line in self.h.output_since(mark) if line.startswith("[#python]")])

    def test_hidden(self):
        self.h.eval("textual_repl.irc.request_output = 'hidden'")
        mark = self.h.mark()
        self.h.eval("await irc['libera'].whois('bob');")
        self.h.expect(r"^<< DROPPED .* 318 tester bob ", mark)
        self.assertFalse([line for line in self.h.output_since(mark) if line.startswith("[Python REPL")])

    def test_textual_default(self):
        self.h.eval("textual_repl.irc.request_output = 'textual'")
        mark = self.h.mark()
        self.h.eval("await irc['libera'].whois('bob');")
        self.assertFalse([line for line in self.h.output_since(mark) if "DROPPED" in line or "Python REPL" in line])

    def test_only_replies_for_the_request_are_claimed(self):
        # The mock server ignores WHO, so the request stays pending until the
        # test sends its terminator: no race with replies arriving late.
        self.h.eval("pending = irc['libera'].request('WHO bob', until='315', key='bob', timeout=10)")
        mark = self.h.mark()
        self.h.send(":irc.example.net 352 tester carol ~c example.org irc.example.net carol H :0 Carol")
        self.h.send(":irc.example.net 352 tester bob ~b example.org irc.example.net bob H :0 Bob")
        self.h.send(":irc.example.net 315 tester bob :End of /WHO list.")
        self.assertEqual(self.h.eval("[e.params[1] for e in await pending]"), "['bob', 'bob']")
        dropped = [line for line in self.h.output_since(mark) if "DROPPED" in line]
        self.assertFalse([line for line in dropped if "carol" in line], "another nick's reply was claimed")
        self.assertEqual(len(dropped), 2, dropped)
