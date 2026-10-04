"""Event handlers, filters, wait_for and requests."""

import unittest

from . import support


class EventTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.h = support.harness()

    def tearDown(self):
        self.h.eval("irc.clear()")

    def test_handler_replies_in_channel_and_privately(self):
        self.h.eval("@irc.on('PRIVMSG', match=r'^!ping\\b')\ndef ping(event):\n    event.reply(f'pong {event.nick}')\n")
        mark = self.h.mark()
        self.h.send(":alice!a@example.org PRIVMSG #python :!ping")
        self.h.send(":bob!b@example.org PRIVMSG tester :!ping")
        self.h.expect(r"^>> PRIVMSG #python :pong alice$", mark)
        self.h.expect(r"^>> \[command\] /msg bob pong bob", mark)

    def test_async_handler(self):
        self.h.eval(
            "@irc.on('JOIN')\n"
            "async def greet(event):\n"
            "    await asyncio.sleep(0.05)\n"
            "    event.client.msg(event.target, f'welcome {event.nick}')\n"
        )
        mark = self.h.mark()
        self.h.send(":carol!c@example.org JOIN #python")
        self.h.expect(r"^>> PRIVMSG #python :welcome carol$", mark)

    def test_reregistering_replaces_a_handler(self):
        self.h.eval("@irc.on('PRIVMSG')\ndef handler(event): pass")
        self.h.eval("@irc.on('NOTICE')\ndef handler(event): pass")
        self.assertEqual(self.h.eval("[h.name for h in irc.handlers]"), "['__main__.handler']")

    def test_filter_drops_lines(self):
        self.h.eval("@irc.filter('PRIVMSG', match='spam')\ndef nospam(event):\n    return False")
        mark = self.h.mark()
        self.h.send(":mallory!m@example.org PRIVMSG #python :buy spam")
        self.h.expect(r"^<< DROPPED .*buy spam$", mark)

    def test_filter_rewrites_lines(self):
        self.h.eval(
            "@irc.filter('PRIVMSG')\n"
            "def shout(event):\n"
            "    return f':{event.hostmask} PRIVMSG {event.target} :{event.text.upper()}'\n"
        )
        mark = self.h.mark()
        self.h.send(":alice!a@example.org PRIVMSG #python :quiet please")
        self.h.expect(r"^<< REWRITTEN PRIVMSG alice .*QUIET PLEASE", mark)

    def test_handler_errors_are_recorded(self):
        self.h.eval("@irc.on('PRIVMSG')\ndef broken(event):\n    raise RuntimeError('boom')")
        self.h.send(":alice!a@example.org PRIVMSG #python :hi")
        self.h.eval("import time\nfor _ in range(50):\n    if irc.errors: break\n    time.sleep(0.05)")
        self.assertEqual(
            self.h.eval("irc.errors[-1][1], repr(irc.errors[-1][2])"), "('__main__.broken', \"RuntimeError('boom')\")"
        )

    def test_wait_for(self):
        output = self.h.eval(
            "pending = irc.wait_for('PONG', client='libera', timeout=5)\n"
            "irc['libera'].send('PING :hello')\n"
            "(await pending).text"
        )
        self.assertEqual(output, "'hello'")
        self.assertEqual(self.h.eval("len(irc._bus._waiters)"), "0")

    def test_wait_for_timeout(self):
        result = self.h.run("await irc.wait_for('KICK', timeout=0.2)")
        self.assertEqual(result.returncode, 1)
        self.assertIn("TimeoutError: no matching event within 0.2 seconds", result.stdout)

    def test_whois(self):
        self.assertEqual(
            self.h.eval(
                "info = await irc['libera'].whois('alice'); info['realname'], info['account'], info['channels']"
            ),
            "('Real Name', 'account', ['@#python', '#textual'])",
        )
        self.assertEqual(self.h.eval("await irc['libera'].whois('nobody') is None"), "True")

    def test_spawned_tasks_are_replaced_by_name(self):
        self.h.eval("async def forever(): await asyncio.sleep(3600)")
        self.h.eval("irc.spawn(forever(), name='forever').result()")
        self.h.eval("first = irc.tasks['forever']; irc.spawn(forever(), name='forever').result()")
        self.assertEqual(self.h.eval("await asyncio.sleep(0.1); first.cancelled(), len(irc.tasks)"), "(True, 1)")
