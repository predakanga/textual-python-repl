"""Replies to REPL requests go to REPL windows instead of the active window."""

import unittest

from . import support


class RequestOutputTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.h = support.harness()

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

    def test_other_whois_replies_are_left_alone(self):
        self.h.eval("pending = irc['libera'].request('WHOIS bob', until='318', key='bob')")
        mark = self.h.mark()
        self.h.send(":irc.example.net 311 tester carol user example.org * :Carol")
        self.assertFalse([line for line in self.h.output_since(mark) if "DROPPED" in line])
        self.h.eval("await pending;")
