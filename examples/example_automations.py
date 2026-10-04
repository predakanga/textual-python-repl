"""Example startup script for Textual's Python REPL.

Copy it into the plugin's startup folder to have it run whenever Textual
launches:

    ~/Library/Group Containers/8482Q6EPL6.com.codeux.apps.textual/
        Library/Application Support/Textual/Python REPL/startup/

Startup scripts run in the same namespace as the REPL, so ``irc`` is
already defined and anything defined here can be inspected or replaced
from an SSH session (re-registering a handler with the same name replaces it).
"""

import asyncio
import re
import time


# Respond to "!ping" in any channel or private message.
@irc.on("PRIVMSG", match=r"^!ping\b")
def ping(event):
    event.reply(f"{event.nick}: pong")


# Show a local notice in the server console whenever someone mentions one
# of these words. Nothing is sent to the server.
WATCH_WORDS = re.compile(r"\b(textual|python repl)\b", re.IGNORECASE)


@irc.on("PRIVMSG", match=WATCH_WORDS)
def watch_words(event):
    if event.is_private:
        return
    event.client.print(f"[watch] {event.target} <{event.nick}> {event.text}")


# Hide join/part/quit noise in one busy channel. Filters run before Textual
# sees the line; returning False drops it.
NOISY_CHANNELS = {"#some-busy-channel"}


@irc.filter("JOIN", "PART")
def hide_noise(event):
    if (event.target or "").lower() in NOISY_CHANNELS:
        return False


# A background task: count PRIVMSGs per channel and report hourly in the console.
message_counts = {}


@irc.on("PRIVMSG")
def count_messages(event):
    if not event.is_private:
        key = (event.client.name, event.target)
        message_counts[key] = message_counts.get(key, 0) + 1


async def hourly_report():
    while True:
        await asyncio.sleep(3600)
        busiest = sorted(message_counts.items(), key=lambda item: -item[1])[:5]
        message_counts.clear()
        for client in irc.clients:
            if client.connected:
                summary = ", ".join(f"{channel} {count}" for (name, channel), count in busiest if name == client.name)
                client.print(f"[{time.strftime('%H:%M')}] busiest channels last hour: {summary or 'none'}")


irc.spawn(hourly_report(), name="hourly_report")
