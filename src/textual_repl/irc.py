"""Pythonic access to Textual's live IRC state.

The ``irc`` object in the REPL namespace is an :class:`IRC` instance::

    irc.clients                         # every configured connection
    c = irc["libera"]                   # by connection name, network or server
    c.channels, c.queries, c.nick
    ch = c["#python"]
    ch.members, ch.topic
    ch.say("hello")                     # also: notice, action, command, print
    c.send("PRIVMSG #python :raw line")

    @irc.on("PRIVMSG", match=r"^!ping\\b")
    def ping(event):
        event.reply("pong")

    event = await irc.wait_for("JOIN", client="libera", timeout=30)
    info = await c.whois("somenick")

Every wrapper keeps the underlying Objective-C object in ``.objc`` for
anything not covered here (PyObjC naming: ``client.objc.sendLine_("...")``).
Reads and actions are performed on Textual's main thread.
"""

from __future__ import annotations

import asyncio
import collections
import concurrent.futures
import contextvars
import inspect
import logging
import re
import sys
import threading
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import objc

from . import mainthread, windows
from .mainthread import call, perform
from .output import current_stream

log = logging.getLogger(__name__)

CHANNEL_STATUS_JOINED = 2

# Where replies to request()/whois() go: "window" (the front end's REPL
# window), "hidden" (only returned to the caller) or "textual" (left for
# Textual to print, which it does in the active window). Set from config.
request_output = "window"


def _str(value) -> str | None:
    return None if value is None else str(value)


def _master():
    return objc.lookUpClass("NSObject").masterController()


def _check_line(text: str) -> str:
    text = str(text)
    if "\r" in text or "\n" in text or "\0" in text:
        raise ValueError("IRC lines cannot contain CR, LF or NUL characters")
    return text


def _lines(text: str) -> list[str]:
    return [line for line in str(text).replace("\r\n", "\n").split("\n") if line]


def _wrap_item(item):
    if item is None:
        return None
    if item.isClient():
        return Client(item)
    return Channel(item)


# --------------------------------------------------------------------------
# Wrappers
# --------------------------------------------------------------------------


class Client:
    """A connection to an IRC server (wraps ``IRCClient``)."""

    __slots__ = ("objc",)

    def __init__(self, client_objc):
        self.objc = client_objc

    # -- identity -----------------------------------------------------------

    @property
    def id(self) -> str:
        """Textual's unique identifier for this connection."""
        return str(self.objc.uniqueIdentifier())

    @property
    def name(self) -> str:
        """The connection name configured in Textual."""
        return call(lambda: str(self.objc.config().connectionName()))

    @property
    def network(self) -> str | None:
        """The network name reported by the server (None until connected)."""
        return call(lambda: _str(self.objc.networkName()))

    @property
    def server(self) -> str | None:
        """The address of the server connected to."""
        return call(lambda: _str(self.objc.serverAddress()))

    @property
    def nick(self) -> str:
        """Our nickname on this connection."""
        return call(lambda: str(self.objc.userNickname()))

    @property
    def connected(self) -> bool:
        return call(lambda: bool(self.objc.isConnected()))

    @property
    def logged_in(self) -> bool:
        return call(lambda: bool(self.objc.isLoggedIn()))

    @property
    def away(self) -> bool:
        return call(lambda: bool(self.objc.userIsAway()))

    def _identity(self) -> tuple[str, ...]:
        def read():
            return tuple(
                str(value).lower()
                for value in (
                    self.objc.uniqueIdentifier(),
                    self.objc.config().connectionName(),
                    self.objc.networkName(),
                    self.objc.serverAddress(),
                )
                if value
            )

        return call(read)

    def matches(self, spec) -> bool:
        """True if ``spec`` (a Client, or a name/network/server/id) names this client."""
        if spec is None:
            return True
        if isinstance(spec, Client):
            return spec == self
        return str(spec).lower() in self._identity()

    # -- channels -------------------------------------------------------------

    @property
    def windows(self) -> list[Channel]:
        """All channels and private message windows."""
        return call(lambda: [Channel(c) for c in self.objc.channelList()])

    @property
    def channels(self) -> list[Channel]:
        """Channel windows (not private messages)."""
        return call(lambda: [Channel(c) for c in self.objc.channelList() if c.isChannel()])

    @property
    def queries(self) -> list[Channel]:
        """Private message windows."""
        return call(lambda: [Channel(c) for c in self.objc.channelList() if c.isPrivateMessage()])

    def channel(self, name: str) -> Channel | None:
        """Find a channel or query window by name (None if Textual has none)."""
        found = call(self.objc.findChannel_, str(name))
        return Channel(found) if found is not None else None

    def __getitem__(self, name: str) -> Channel:
        found = self.channel(name)
        if found is None:
            raise KeyError(name)
        return found

    def __contains__(self, name: str) -> bool:
        return self.channel(name) is not None

    def __iter__(self):
        return iter(self.windows)

    # -- users ----------------------------------------------------------------

    def user(self, nick: str) -> User | None:
        """Textual's record of a user that shares a channel with us."""

        def read():
            found = self.objc.findUser_(str(nick))
            return User.from_objc(found) if found is not None else None

        return call(read)

    @property
    def users(self) -> list[User]:
        """Every user Textual is tracking on this connection."""
        return call(lambda: [User.from_objc(u) for u in self.objc.userList()])

    # -- actions --------------------------------------------------------------

    def send(self, line: str) -> None:
        """Send a raw line to the server, e.g. ``send("WHO #channel")``."""
        line = _check_line(line)
        perform(self.objc.sendLine_, line)

    def command(self, text: str, target: str | Channel | None = None) -> None:
        """Run a Textual input command as if typed, e.g. ``command("/topic #x hi")``.

        ``target`` sets the channel that commands like ``/me`` act on.
        """
        text = _check_line(text)
        if isinstance(target, Channel):
            target = target.name
        perform(self.objc.sendCommand_completeTarget_target_, text, target is not None, target)

    def msg(self, target: str, text: str) -> None:
        """Send a PRIVMSG. Multi-line text is sent line by line."""
        self._say("privmsg", target, text)

    def notice(self, target: str, text: str) -> None:
        """Send a NOTICE. Multi-line text is sent line by line."""
        self._say("notice", target, text)

    def action(self, target: str, text: str) -> None:
        """Send a CTCP ACTION (``/me``)."""
        self._say("action", target, text)

    def _say(self, kind: str, target, text: str) -> None:
        target = target.name if isinstance(target, Channel) else _check_line(target)
        lines = _lines(text)

        def run():
            window = self.objc.findChannel_(target)
            for line in lines:
                if window is not None:
                    selector = {
                        "privmsg": self.objc.sendPrivmsg_toChannel_,
                        "notice": self.objc.sendNotice_toChannel_,
                        "action": self.objc.sendAction_toChannel_,
                    }[kind]
                    selector(line, window)
                elif kind == "action":
                    self.objc.sendLine_(f"PRIVMSG {target} :\x01ACTION {line}\x01")
                else:
                    self.objc.sendCommand_completeTarget_target_(
                        f"/{'msg' if kind == 'privmsg' else 'notice'} {target} {line}", False, None
                    )

        perform(run)

    def ctcp(self, target: str, command: str, text: str | None = None) -> None:
        perform(self.objc.sendCTCPQuery_command_text_, _check_line(target), _check_line(command), text)

    def join(self, channel: str, key: str | None = None) -> None:
        perform(self.objc.joinUnlistedChannel_password_, _check_line(channel), key)

    def part(self, channel: str, reason: str | None = None) -> None:
        perform(self.objc.partUnlistedChannel_withComment_, _check_line(channel), reason)

    def set_nick(self, nick: str) -> None:
        perform(self.objc.changeNickname_, _check_line(nick))

    def connect(self) -> None:
        perform(self.objc.connect)

    def disconnect(self, reason: str | None = None) -> None:
        if reason is None:
            perform(self.objc.quit)
        else:
            perform(self.objc.quitWithComment_, _check_line(reason))

    def print(self, text: str, channel: str | Channel | None = None) -> None:
        """Show text locally in Textual (never sent to the server).

        Goes to ``channel`` if given, otherwise the server console.
        """
        if isinstance(channel, str):
            channel = self[channel]

        def run():
            for line in str(text).splitlines() or [""]:
                if channel is None:
                    self.objc.printDebugInformationToConsole_(line)
                else:
                    self.objc.printDebugInformation_inChannel_(line, channel.objc)

        perform(run)

    # -- request / response ---------------------------------------------------

    def request(self, line: str, until, collect=None, key: str | None = None, timeout: float = 15.0):
        """Send a raw line and gather the server's reply.

        Collects events from this client (only those whose command is in
        ``collect`` if given, and whose second parameter equals ``key`` if
        given) until one whose command is in ``until`` arrives. Returns an
        awaitable list of :class:`Event`::

            replies = await c.request("WHOIS bob", until="318", key="bob")

        Textual doesn't print the replies itself. Depending on the
        preferences they're shown in this session's REPL window (the
        default) or not at all.
        """
        if _bus is None:
            raise RuntimeError("The event bus is not running")
        line = _check_line(line)
        mode = request_output
        window = windows.current_window.get() if mode == "window" else None
        waiter = _bus.add_collector(
            self, until=until, collect=collect, key=key, suppress=mode in ("window", "hidden"), window=window
        )
        try:
            if window is not None:
                windows.print_lines(self.objc, [f"» {line}"], window)
            self.send(line)
        except BaseException:
            _bus.remove_waiter(waiter)
            raise
        return _bus.await_waiter(waiter, timeout)

    async def whois(self, nick: str, timeout: float = 15.0) -> dict | None:
        """WHOIS ``nick`` and return the reply as a dict (None if no such nick)."""
        replies = await self.request(f"WHOIS {nick}", until="318", key=nick, timeout=timeout)
        info: dict[str, Any] = {"nick": nick, "channels": [], "replies": replies}
        for event in replies:
            p = event.params
            if event.command in ("401", "402"):
                return None
            if event.command == "311" and len(p) >= 6:
                info.update(nick=p[1], user=p[2], host=p[3], realname=p[5])
            elif event.command == "312" and len(p) >= 3:
                info["server"] = p[2]
            elif event.command == "317" and len(p) >= 4:
                info["idle"] = int(p[2])
                info["signon"] = int(p[3])
            elif event.command == "319" and len(p) >= 3:
                info["channels"].extend(p[2].split())
            elif event.command == "330" and len(p) >= 3:
                info["account"] = p[2]
            elif event.command == "301" and len(p) >= 3:
                info["away"] = p[2]
            elif event.command == "313":
                info["operator"] = True
            elif event.command == "671":
                info["secure"] = True
        return info

    # -- dunder ---------------------------------------------------------------

    def __eq__(self, other):
        return isinstance(other, Client) and other.id == self.id

    def __hash__(self):
        return hash(self.id)

    def __repr__(self):
        def read():
            name = self.objc.config().connectionName()
            state = "connected" if self.objc.isConnected() else "disconnected"
            return f"<Client {str(name)!r} nick={str(self.objc.userNickname())!r} {state}>"

        return call(read)


class Channel:
    """A channel or private message window (wraps ``IRCChannel``)."""

    __slots__ = ("objc",)

    def __init__(self, channel_objc):
        self.objc = channel_objc

    @property
    def id(self) -> str:
        return str(self.objc.uniqueIdentifier())

    @property
    def client(self) -> Client:
        return Client(call(self.objc.associatedClient))

    @property
    def name(self) -> str:
        return call(lambda: str(self.objc.name()))

    @property
    def topic(self) -> str | None:
        return call(lambda: _str(self.objc.topic()))

    @property
    def is_query(self) -> bool:
        """True for private message windows."""
        return call(lambda: bool(self.objc.isPrivateMessage()))

    @property
    def joined(self) -> bool:
        return call(lambda: self.objc.status() == CHANNEL_STATUS_JOINED)

    @property
    def members(self) -> list[Member]:
        """Snapshot of the member list, sorted by rank like Textual's."""

        def read():
            info = self.objc.memberInfo()
            if info is None:
                return []
            return [Member.from_objc(m) for m in (info.memberList() or [])]

        return call(read)

    @property
    def nicks(self) -> list[str]:
        return [m.nick for m in self.members]

    def member(self, nick: str) -> Member | None:
        def read():
            info = self.objc.memberInfo()
            found = info.findMember_(str(nick)) if info is not None else None
            return Member.from_objc(found) if found is not None else None

        return call(read)

    def __contains__(self, nick: str) -> bool:
        return self.member(nick) is not None

    def __iter__(self):
        return iter(self.members)

    def __len__(self):
        return call(lambda: int(self.objc.numberOfMembers()))

    # -- actions --------------------------------------------------------------

    def say(self, text: str) -> None:
        """Send a PRIVMSG to this channel. Multi-line text is sent line by line."""
        self.client.msg(self.name, text)

    msg = say

    def notice(self, text: str) -> None:
        self.client.notice(self.name, text)

    def action(self, text: str) -> None:
        self.client.action(self.name, text)

    def command(self, text: str) -> None:
        """Run a Textual input command targeting this channel, e.g. ``"/me waves"``."""
        self.client.command(text, target=self.name)

    def print(self, text: str) -> None:
        """Show text locally in this window (never sent to the server)."""
        self.client.print(text, channel=self)

    def part(self, reason: str | None = None) -> None:
        self.client.part(self.name, reason)

    def set_topic(self, topic: str) -> None:
        self.client.send(f"TOPIC {self.name} :{_check_line(topic)}")

    def mode(self, modes: str, *params: str) -> None:
        self.client.send(" ".join(["MODE", self.name, modes, *map(str, params)]))

    def kick(self, nick: str, reason: str | None = None) -> None:
        line = f"KICK {self.name} {nick}"
        if reason:
            line += f" :{reason}"
        self.client.send(line)

    def __eq__(self, other):
        return isinstance(other, Channel) and other.id == self.id

    def __hash__(self):
        return hash(self.id)

    def __repr__(self):
        def read():
            client = self.objc.associatedClient()
            client_name = str(client.config().connectionName()) if client is not None else "?"
            if self.objc.isPrivateMessage():
                return f"<Query {str(self.objc.name())!r} on {client_name!r}>"
            state = "joined" if self.objc.status() == CHANNEL_STATUS_JOINED else "not joined"
            return f"<Channel {self.objc.name()!s} on {client_name!r} {self.objc.numberOfMembers()} members, {state}>"

        return call(read)


@dataclass(frozen=True)
class User:
    """Snapshot of what Textual knows about a user."""

    nick: str
    username: str | None = None
    address: str | None = None
    realname: str | None = None
    away: bool = False
    ircop: bool = False

    @property
    def hostmask(self) -> str:
        return f"{self.nick}!{self.username or '*'}@{self.address or '*'}"

    @classmethod
    def from_objc(cls, user) -> User:
        return cls(
            nick=str(user.nickname()),
            username=_str(user.username()),
            address=_str(user.address()),
            realname=_str(user.realName()),
            away=bool(user.isAway()),
            ircop=bool(user.isIRCop()),
        )


@dataclass(frozen=True)
class Member:
    """Snapshot of a channel member: ``modes`` like ``"ov"``, ``mark`` like ``"@"``."""

    nick: str
    modes: str = ""
    mark: str = ""
    user: User | None = field(default=None, repr=False)

    @property
    def op(self) -> bool:
        return any(m in self.modes for m in "qao")

    @property
    def voice(self) -> bool:
        return "v" in self.modes

    def __str__(self):
        return f"{self.mark}{self.nick}"

    @classmethod
    def from_objc(cls, member) -> Member:
        user = User.from_objc(member.user())
        return cls(nick=user.nick, modes=str(member.modes() or ""), mark=str(member.mark() or ""), user=user)


# --------------------------------------------------------------------------
# Events
# --------------------------------------------------------------------------


class Event:
    """A message received from an IRC server.

    ``command`` is upper case (numerics are three digit strings, e.g.
    ``"001"``) and ``params`` is the list of parameters, so for
    ``:nick!user@host PRIVMSG #chan :hello`` you get ``nick == "nick"``,
    ``target == "#chan"`` and ``text == "hello"``.
    """

    def __init__(self, message_objc, client_objc):
        self.objc = message_objc
        self.client = Client(client_objc)
        self.command: str = str(message_objc.command()).upper()
        self.params: list[str] = [str(p) for p in (message_objc.params() or [])]
        self.nick: str | None = _str(message_objc.senderNickname())
        self.username: str | None = _str(message_objc.senderUsername())
        self.address: str | None = _str(message_objc.senderAddress())
        self.hostmask: str | None = _str(message_objc.senderHostmask())
        self.from_server: bool = bool(message_objc.senderIsServer())
        tags = message_objc.messageTags()
        self.tags: dict[str, str] = {str(k): str(v) for k, v in tags.items()} if tags else {}
        self.time: float = float(message_objc.receivedAt().timeIntervalSince1970())
        self.historic: bool = bool(message_objc.isHistoric())
        self.match: re.Match | None = None

    @property
    def numeric(self) -> int | None:
        return int(self.command) if self.command.isdigit() else None

    @property
    def target(self) -> str | None:
        """First parameter: the channel or nick a PRIVMSG/NOTICE/JOIN/... is for."""
        return self.params[0] if self.params else None

    @property
    def text(self) -> str | None:
        """Trailing parameter: message text for PRIVMSG/NOTICE, reason for PART/QUIT..."""
        return self.params[-1] if self.params else None

    @property
    def ctcp(self) -> tuple[str, str] | None:
        """``("ACTION", "waves")`` for CTCP messages, otherwise None."""
        text = self.text or ""
        if self.command in ("PRIVMSG", "NOTICE") and text.startswith("\x01"):
            command, _, args = text.strip("\x01").partition(" ")
            return command.upper(), args
        return None

    @property
    def is_private(self) -> bool:
        """True for messages sent directly to us rather than to a channel."""
        target = self.target
        return bool(target) and not call(lambda: bool(self.client.objc.stringIsChannelName_(target)))

    @property
    def channel(self) -> Channel | None:
        """The channel window this event concerns, if Textual has one."""
        if not self.target:
            return None
        if self.is_private:
            return self.client.channel(self.nick) if self.nick else None
        return self.client.channel(self.target)

    @property
    def reply_target(self) -> str | None:
        """Where a reply should go: the channel, or the sender for private messages."""
        return self.nick if self.is_private else self.target

    def reply(self, text: str) -> None:
        """Reply in the channel (or privately, for a private message)."""
        self.client.msg(self.reply_target, text)

    def reply_notice(self, text: str) -> None:
        self.client.notice(self.reply_target, text)

    @property
    def line(self) -> str:
        """Approximate raw IRC line (reconstructed)."""
        parts = []
        if self.hostmask or self.nick:
            parts.append(f":{self.hostmask or self.nick}")
        parts.append(self.command)
        if self.params:
            *middle, last = self.params
            parts.extend(middle)
            parts.append(f":{last}")
        return " ".join(parts)

    def __repr__(self):
        who = self.nick or "server"
        if self.command in ("PRIVMSG", "NOTICE"):
            return f"<Event {self.command} {who} → {self.target}: {self.text!r}>"
        return f"<Event {self.command} from {who} {self.params!r}>"


# --------------------------------------------------------------------------
# Event bus
# --------------------------------------------------------------------------


def _normalize_commands(commands) -> frozenset[str] | None:
    """None means 'every command'."""
    result: set[str] = set()

    def add(value):
        if isinstance(value, (list, tuple, set, frozenset)):
            for item in value:
                add(item)
        elif isinstance(value, int):
            result.add(f"{value:03d}")
        else:
            result.add(str(value).upper())

    add(commands or ())
    if not result or "*" in result:
        return None
    return frozenset(result)


@dataclass
class _Matcher:
    commands: frozenset[str] | None = None
    client: Any = None
    target: frozenset[str] | None = None
    pattern: re.Pattern | None = None

    @classmethod
    def build(cls, commands=None, client=None, target=None, match=None) -> _Matcher:
        if isinstance(target, (str, Channel)):
            target = [target]
        targets = frozenset((t.name if isinstance(t, Channel) else str(t)).lower() for t in target) if target else None
        if isinstance(match, str):
            match = re.compile(match)
        return cls(_normalize_commands(commands), client, targets, match)

    def matches(self, event: Event) -> re.Match | bool | None:
        if self.commands is not None and event.command not in self.commands:
            return False
        if self.target is not None and (event.target or "").lower() not in self.target:
            return False
        if self.client is not None and not event.client.matches(self.client):
            return False
        if self.pattern is not None:
            return self.pattern.search(event.text or "")
        return True

    def describe(self) -> str:
        parts = [",".join(sorted(self.commands)) if self.commands else "*"]
        if self.client is not None:
            parts.append(f"client={self.client.name if isinstance(self.client, Client) else self.client}")
        if self.target:
            parts.append(f"target={','.join(sorted(self.target))}")
        if self.pattern is not None:
            parts.append(f"match={self.pattern.pattern!r}")
        return " ".join(parts)


@dataclass
class Handler:
    name: str
    func: Callable
    matcher: _Matcher
    context: contextvars.Context
    kind: str  # "handler" or "filter"
    calls: int = 0
    errors: int = 0

    def __repr__(self):
        return f"<{self.kind} {self.name}: {self.matcher.describe()} calls={self.calls} errors={self.errors}>"


class _Waiter:
    """Waits for one matching event (``until`` is None) or collects replies."""

    def __init__(
        self,
        matcher: _Matcher,
        check=None,
        until=None,
        key: str | None = None,
        suppress: bool = False,
        window: str | None = None,
    ):
        self.matcher = matcher
        self.check = check
        self.until = _normalize_commands(until) if until is not None else None
        self.key = key.lower() if key is not None else None
        self.suppress = suppress
        self.window = window
        self.collected: list[Event] = []
        self.future: concurrent.futures.Future = concurrent.futures.Future()

    @property
    def is_collector(self) -> bool:
        return self.until is not None

    def feed(self, event: Event) -> None:
        if not self.future.done() and self.matcher.matches(event) and (self.check is None or self.check(event)):
            self.future.set_result(event)

    def collect(self, event: Event) -> bool:
        """Collector only: take ``event`` if it's part of the reply."""
        if self.future.done() or not event.client.matches(self.matcher.client):
            return False
        if self.matcher.commands is not None and event.command not in self.matcher.commands | self.until:
            return False
        if self.key is not None and (len(event.params) < 2 or event.params[1].lower() != self.key):
            return False
        self.collected.append(event)
        if event.command in self.until:
            self.future.set_result(self.collected)
        return True


def _handler_name(func, name: str | None) -> str:
    if name:
        return name
    module = getattr(func, "__module__", None) or "?"
    qualname = getattr(func, "__qualname__", None) or repr(func)
    return f"{module}.{qualname}"


class EventBus:
    """Dispatches server input to handlers, filters and waiters."""

    def __init__(self, loop: asyncio.AbstractEventLoop, set_intercepting: Callable[[bool], None]):
        self.loop = loop
        self._set_intercepting = set_intercepting
        self._lock = threading.Lock()
        self._handlers: dict[str, Handler] = {}
        self._filters: dict[str, Handler] = {}
        self._waiters: list[_Waiter] = []
        self._snapshot = ((), (), ())
        self.tasks: dict[str, asyncio.Task] = {}
        self.errors: collections.deque = collections.deque(maxlen=50)

    # -- registration -------------------------------------------------------

    def _changed(self) -> None:
        self._snapshot = (tuple(self._filters.values()), tuple(self._handlers.values()), tuple(self._waiters))
        self._set_intercepting(any(self._snapshot))

    def register(self, kind: str, func, commands, client, target, match, name) -> Handler:
        handler = Handler(
            name=_handler_name(func, name),
            func=func,
            matcher=_Matcher.build(commands, client, target, match),
            context=contextvars.copy_context(),
            kind=kind,
        )
        with self._lock:
            table = self._filters if kind == "filter" else self._handlers
            table.pop(handler.name, None)  # re-registering replaces (and re-orders)
            table[handler.name] = handler
            self._changed()
        return handler

    def unregister(self, func_or_name) -> bool:
        with self._lock:
            removed = False
            for table in (self._handlers, self._filters):
                for key, handler in list(table.items()):
                    if func_or_name in (key, handler.func, handler):
                        del table[key]
                        removed = True
            self._changed()
        return removed

    def clear(self) -> None:
        with self._lock:
            self._handlers.clear()
            self._filters.clear()
            self._changed()
        for task in list(self.tasks.values()):
            self.loop.call_soon_threadsafe(task.cancel)

    @property
    def handlers(self) -> list[Handler]:
        filters, handlers, _ = self._snapshot
        return [*filters, *handlers]

    def add_waiter(self, waiter: _Waiter) -> _Waiter:
        with self._lock:
            self._waiters.append(waiter)
            self._changed()
        return waiter

    def add_collector(self, client: Client, until, collect=None, key=None, suppress=False, window=None) -> _Waiter:
        return self.add_waiter(
            _Waiter(_Matcher.build(collect, client), until=until, key=key, suppress=suppress, window=window)
        )

    def remove_waiter(self, waiter: _Waiter) -> None:
        with self._lock:
            if waiter in self._waiters:
                self._waiters.remove(waiter)
                self._changed()

    async def await_waiter(self, waiter: _Waiter, timeout: float | None):
        try:
            return await asyncio.wait_for(asyncio.wrap_future(waiter.future), timeout)
        except TimeoutError:
            raise TimeoutError(f"no matching event within {timeout} seconds") from None
        finally:
            self.remove_waiter(waiter)

    # -- background tasks ---------------------------------------------------

    def spawn(self, coro, name: str | None = None) -> concurrent.futures.Future:
        """Run a coroutine on the IRC event loop in the background."""
        if not inspect.isawaitable(coro):
            raise TypeError("spawn() needs a coroutine, e.g. spawn(main()) not spawn(main)")
        name = name or getattr(coro, "__qualname__", None) or repr(coro)
        context = contextvars.copy_context()
        started: concurrent.futures.Future = concurrent.futures.Future()

        def start():
            previous = self.tasks.get(name)
            if previous is not None and not previous.done():
                previous.cancel()
            task = self.loop.create_task(self._guard(name, coro), name=name, context=context)
            self.tasks[name] = task

            def finished(t, name=name):
                if self.tasks.get(name) is t:
                    del self.tasks[name]

            task.add_done_callback(finished)
            started.set_result(task)

        self.loop.call_soon_threadsafe(start)
        return started

    async def _guard(self, name: str, awaitable):
        try:
            return await awaitable
        except asyncio.CancelledError:
            raise
        except Exception:
            self._report(name)

    def _report(self, name: str) -> None:
        exc = sys.exc_info()[1]
        log.error("%s failed", name, exc_info=True)
        self.errors.append((time.time(), name, exc))
        # If whoever registered this is still connected, show them too.
        stream = current_stream.get()
        if stream is not None and not stream.closed:
            print(f"[textual_repl] {name} failed:", file=sys.stderr)
            traceback.print_exc()

    # -- dispatch -----------------------------------------------------------

    def intercept(self, message_objc, client_objc):
        """Called synchronously on a connection thread for every server line.

        Returns the message to process, a replacement, or None to drop it.
        """
        filters, handlers, waiters = self._snapshot
        event = None

        # Replies to REPL requests are claimed here, before Textual prints
        # them in whatever window happens to be active.
        collectors = [w for w in waiters if w.is_collector]
        if collectors:
            event = Event(message_objc, client_objc)
            claimed = False
            for waiter in collectors:
                if not waiter.collect(event):
                    continue
                if waiter.future.done():
                    self.remove_waiter(waiter)
                if waiter.suppress:
                    claimed = True
                if waiter.window is not None:
                    text = windows.format_reply(event)
                    if text is not None:
                        with mainthread.filter_context():  # never block this thread on the main thread
                            windows.print_lines(client_objc, [text], waiter.window)
            if claimed:
                if handlers or len(collectors) < len(waiters):
                    self.loop.call_soon_threadsafe(self._dispatch, message_objc, client_objc)
                return None

        if filters:
            event = event or Event(message_objc, client_objc)
            with mainthread.filter_context():
                for handler in filters:
                    outcome = handler.context.copy().run(self._run_filter, handler, event)
                    if outcome is False:
                        return None
                    if isinstance(outcome, str):
                        replacement = (
                            objc.lookUpClass("IRCMessage").alloc().initWithLine_onClient_(outcome, client_objc)
                        )
                        if replacement is None:
                            return None
                        message_objc = replacement
                        event = Event(message_objc, client_objc)

        if handlers or waiters:
            self.loop.call_soon_threadsafe(self._dispatch, message_objc, client_objc)

        return message_objc

    def _run_filter(self, handler: Handler, event: Event):
        match = handler.matcher.matches(event)
        if not match:
            return None
        event.match = match if isinstance(match, re.Match) else None
        handler.calls += 1
        try:
            return handler.func(event)
        except Exception:
            handler.errors += 1
            self._report(handler.name)
            return None

    def _dispatch(self, message_objc, client_objc) -> None:
        try:
            event = Event(message_objc, client_objc)
        except Exception:
            log.exception("Could not wrap incoming message")
            return

        _, handlers, waiters = self._snapshot

        for waiter in waiters:
            if waiter.is_collector:
                continue  # fed synchronously in intercept()
            try:
                waiter.feed(event)
            except Exception as exc:
                if not waiter.future.done():
                    waiter.future.set_exception(exc)
            if waiter.future.done():
                # Normally await_waiter() does this, but not if nobody awaits.
                self.remove_waiter(waiter)

        for handler in handlers:
            handler.context.copy().run(self._run_handler, handler, event)

    def _run_handler(self, handler: Handler, event: Event) -> None:
        try:
            match = handler.matcher.matches(event)
            if not match:
                return
            event.match = match if isinstance(match, re.Match) else None
            handler.calls += 1
            result = handler.func(event)
            if inspect.isawaitable(result):
                self.loop.create_task(self._guard_handler(handler, result), context=contextvars.copy_context())
        except Exception:
            handler.errors += 1
            self._report(handler.name)

    async def _guard_handler(self, handler: Handler, awaitable):
        try:
            await awaitable
        except asyncio.CancelledError:
            raise
        except Exception:
            handler.errors += 1
            self._report(handler.name)


_bus: EventBus | None = None


# --------------------------------------------------------------------------
# Entry point object
# --------------------------------------------------------------------------


class IRC:
    """Entry point to Textual's IRC state. Try ``irc.clients`` or ``help(irc)``.

    Lookup:     irc.clients, irc["name"], irc.find("name"), irc.channel("net/#chan"),
                irc.selected, irc.selected_client
    Events:     @irc.on(...), @irc.filter(...), irc.off(fn), irc.handlers,
                await irc.wait_for(...), irc.spawn(coro), irc.tasks, irc.errors
    Raw:        irc.objc (IRCWorld), irc.master (TXMasterController)
    """

    def __init__(self, bus: EventBus):
        self._bus = bus

    # -- objects ------------------------------------------------------------

    @property
    def master(self):
        """Textual's ``TXMasterController`` (raw Objective-C object)."""
        return _master()

    @property
    def objc(self):
        """Textual's ``IRCWorld`` (raw Objective-C object)."""
        return _master().world()

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        """The asyncio loop that handlers and spawned tasks run on."""
        return self._bus.loop

    @property
    def clients(self) -> list[Client]:
        return call(lambda: [Client(c) for c in _master().world().clientList()])

    def find(self, name: str) -> Client | None:
        """Find a client by connection name, network name, server address or id."""
        for client in self.clients:
            if client.matches(name):
                return client
        return None

    def __getitem__(self, key) -> Client:
        if isinstance(key, int):
            return self.clients[key]
        found = self.find(key)
        if found is None:
            raise KeyError(key)
        return found

    def __iter__(self):
        return iter(self.clients)

    def __len__(self):
        return call(lambda: int(_master().world().clientCount()))

    def channel(self, spec: str) -> Channel | None:
        """``"network/#chan"`` or just ``"#chan"`` (first match on any client)."""
        client_name, _, channel_name = spec.rpartition("/")
        clients = [self[client_name]] if client_name else self.clients
        for client in clients:
            found = client.channel(channel_name)
            if found is not None:
                return found
        return None

    @property
    def selected(self) -> Client | Channel | None:
        """Whatever is selected in Textual's server list."""
        return call(lambda: _wrap_item(_master().mainWindow().selectedItem()))

    @property
    def selected_client(self) -> Client | None:
        found = call(lambda: _master().mainWindow().selectedClient())
        return Client(found) if found is not None else None

    # -- events -------------------------------------------------------------

    def on(self, *commands, client=None, target=None, match=None, name: str | None = None):
        """Decorator registering an event handler.

        ``commands`` are IRC commands or numerics (``"PRIVMSG"``, ``"JOIN"``,
        ``"001"``, ``433``); none or ``"*"`` means everything. ``client``
        restricts to one connection, ``target`` to a channel/nick (first
        param), ``match`` is a regex searched in ``event.text`` (the match
        object is available as ``event.match``).

        Handlers may be plain functions or coroutines and run on the IRC
        event loop thread, so don't block in plain functions. Registering a
        handler with the same name (by default ``module.qualname``) replaces
        the old one, so you can just re-run a cell to update it.
        """
        if len(commands) == 1 and callable(commands[0]) and not isinstance(commands[0], (str, int)):
            return self.on()(commands[0])

        def decorator(func):
            self._bus.register("handler", func, commands, client, target, match, name)
            return func

        return decorator

    def filter(self, *commands, client=None, target=None, match=None, name: str | None = None):
        """Decorator registering a synchronous filter for incoming lines.

        Filters run on the connection thread *before* Textual processes the
        line. Return ``False`` to drop it, a string to replace it with a
        different raw line, or anything else to let it through. Keep them
        fast: Textual waits for them. Actions taken inside a filter
        (sending, printing) are queued rather than waited for.
        """

        def decorator(func):
            self._bus.register("filter", func, commands, client, target, match, name)
            return func

        return decorator

    def off(self, func_or_name) -> bool:
        """Remove a handler or filter (by function, Handler, or name)."""
        return self._bus.unregister(func_or_name)

    def clear(self) -> None:
        """Remove all handlers and filters and cancel spawned tasks."""
        self._bus.clear()

    @property
    def handlers(self) -> list[Handler]:
        return self._bus.handlers

    @property
    def tasks(self) -> dict[str, asyncio.Task]:
        return dict(self._bus.tasks)

    @property
    def errors(self) -> list:
        """Recent handler/task failures: (timestamp, name, exception)."""
        return list(self._bus.errors)

    def wait_for(
        self,
        *commands,
        check: Callable[[Event], bool] | None = None,
        client=None,
        target=None,
        match=None,
        timeout: float | None = None,
    ):
        """Wait for the next matching event and return it (awaitable).

        Registration happens immediately, so it's safe to send a command
        after calling this and before awaiting::

            pending = irc.wait_for("PONG", client=c, timeout=10)
            c.send("PING :hello")
            event = await pending
        """
        waiter = self._bus.add_waiter(_Waiter(_Matcher.build(commands, client, target, match), check=check))
        return self._bus.await_waiter(waiter, timeout)

    def spawn(self, coro, name: str | None = None):
        """Run a coroutine in the background on the IRC event loop.

        A task with the same name replaces (cancels) the previous one.
        Returns a concurrent.futures.Future resolving to the asyncio.Task.
        """
        return self._bus.spawn(coro, name)

    def __repr__(self):
        try:
            clients = self.clients
        except Exception:
            return "<IRC>"
        return f"<IRC {len(clients)} clients: {', '.join(repr(c.name) for c in clients)}>"


def create(loop: asyncio.AbstractEventLoop, set_intercepting: Callable[[bool], None]) -> IRC:
    global _bus
    _bus = EventBus(loop, set_intercepting)
    return IRC(_bus)
