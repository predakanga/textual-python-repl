# Python REPL for Textual

[![CI](https://github.com/predakanga/textual-python-repl/actions/workflows/ci.yml/badge.svg)](https://github.com/predakanga/textual-python-repl/actions/workflows/ci.yml)

An IPython shell running *inside* the [Textual](https://github.com/Codeux-Software/Textual) IRC client, with live access to its IRC state, served over SSH. You can use it interactively to automate IRC, and scripts or AI agents can run one-off commands with it.

```
$ ssh -p 2323 127.0.0.1
Textual Python REPL — Python 3.13.15, IPython 9.17.1

In [1]: irc.clients
Out[1]: [<Client 'Libera' nick='me' connected>, <Client 'OFTC' nick='me' disconnected>]

In [2]: py = irc["libera"]["#python"]; len(py), py.topic
Out[2]: (1342, 'Python programming language | ...')

In [3]: @irc.on("PRIVMSG", target="#python", match=r"^!ping\b")
   ...: def ping(event):
   ...:     event.reply("pong")

In [4]: info = await irc["libera"].whois("someone")
```

```
$ ssh -p 2323 127.0.0.1 '[c.name for c in irc["libera"].channels]'
['#python', '#textual']
```

## Requirements

- Textual 7.2.4 or later
- macOS 11 or later on Apple silicon

## Installing

Textual keeps its plugins inside its sandbox container, and macOS only lets Textual itself write there. So installing means handing the plugin to Textual's built-in extension installer.

### From a release

1. Download `TextualPythonREPL-<version>.zip` from the [latest release](https://github.com/predakanga/textual-python-repl/releases/latest) and unzip it.
2. Open `PythonREPL.bundle` with Textual: drag it onto Textual's Dock icon, or run `open -a Textual PythonREPL.bundle`. Confirm the install, then restart Textual.
3. The first time Textual loads it, macOS may say it can't verify the plugin is free of malware. See [Approving the plugin](#approving-the-plugin).
4. Type `/pyrepl` in Textual's input field to check that the REPL is running.
5. Open **Preferences → Addons → Python REPL** and paste your SSH public key (for example the contents of `~/.ssh/id_ed25519.pub`) into **Authorized keys**. Then click **Apply**.
6. Connect with `ssh -p 2323 127.0.0.1`.

### From source

You need the Xcode command line tools (`xcode-select --install`). Full Xcode isn't required.

```sh
git clone https://github.com/predakanga/textual-python-repl.git
cd textual-python-repl
make install    # builds, then opens the bundle with Textual
```

Confirm the install in Textual and restart it, then carry on from step 3 above.

### Approving the plugin

macOS quarantines the files Textual copies into its sandbox. When Textual next loads the plugin, Gatekeeper checks it. Builds that aren't notarized with an Apple Developer ID fail that check, and macOS warns that it can't verify the plugin is free of malware. To allow it:

1. Click **Done** (not **Move to Bin**).
2. In **System Settings → Privacy & Security**, find the message about the blocked plugin and click **Allow Anyway**.
3. Restart Textual and click **Open Anyway** when asked again.

That approval lasts until you install a different version. Notarized builds skip it.

The build downloads a standalone Python from [python-build-standalone](https://github.com/astral-sh/python-build-standalone) and checks it against a pinned SHA-256. It then installs the hash-locked dependencies from `requirements.txt`. Downloads are cached in `.cache/`.

The installed bundle is about 165 MB, nearly all of it the Python runtime and its packages. Textual is sandboxed, so the interpreter has to live inside the plugin.

### Uninstalling

Quit Textual and delete `PythonREPL.bundle` from `~/Library/Group Containers/8482Q6EPL6.com.codeux.apps.textual/Library/Application Support/Textual/Extensions/` in Finder (macOS may ask whether Finder can access Textual's data). The plugin's settings live next to that folder, in `Python REPL/`.

## Using it

| Command | What it does |
| --- | --- |
| `ssh -p 2323 127.0.0.1` | Interactive IPython with completion, `?` help, history and Ctrl-C |
| `ssh -p 2323 127.0.0.1 'expr'` | Runs the code, prints its output and the value of the last expression, then exits with status 0, or 1 on error |
| `ssh -p 2323 127.0.0.1 < script.py` | Same, with the code read from stdin |

The first time you connect, ssh asks you to accept the REPL's host key. To shorten the command, add this to `~/.ssh/config`:

```
Host textual
    HostName 127.0.0.1
    Port 2323
    HostKeyAlias textual-python-repl
```

Everything shares one namespace: interactive sessions, exec requests, startup scripts, and `/py` typed into Textual. Anything defined in one is visible in the others.

Inside Textual:

- `/py <code>` runs code and prints the output in the current window, e.g. `/py irc.selected.members[:5]`.
- `/pyrepl` shows status: the address, active sessions, handlers, tasks, and any startup script errors.
- **Preferences → Addons → Python REPL** turns the SSH server on or off. It also sets the address and port, shows live status and the `ssh` command, edits the authorized keys, and chooses where request replies go. Changes apply immediately, and open sessions survive a restart of the listener.

### REPL windows

Replies to requests made from Python, such as `await c.whois(nick)` or `await c.request(...)`, don't appear in whatever window is active. They go to a REPL window on that connection, and Textual's own copy is suppressed:

- Each interactive SSH session gets its own window: "Python REPL 1", "Python REPL 2", and so on. Numbers are reused once a session ends.
- Exec requests and `/py` share "Python REPL".

These are Textual utility windows, so they're never sent to the server, saved, or logged. In the preferences pane you can hide these replies entirely, or leave them for Textual to print in the active window.

## The `irc` API

`help(irc)`, `help(textual_repl.irc.Client)`, and so on have the details. In summary:

```python
irc.clients                      # [Client, ...]
irc["libera"]                    # by connection name, network name, server address or id
irc.channel("libera/#python")    # or irc.channel("#python") for the first match
irc.selected                     # the selected Client or Channel in Textual's window

c = irc["libera"]
c.name, c.network, c.server, c.nick, c.connected
c.channels, c.queries, c.windows, c["#python"], c.channel("#python")
c.user("nick")                   # User snapshot (nick, username, address, realname, away, ircop)
c.msg(target, text)              # also notice(), action(), ctcp()
c.send("WHO #python")            # raw line (CR/LF are rejected)
c.command("/topic #x new topic") # anything you could type in Textual
c.join("#chan"), c.part("#chan", "bye"), c.set_nick("me_"), c.connect(), c.disconnect("bye")
c.print("only shown locally")    # server console, or c.print(text, channel="#x")
await c.whois("nick")            # dict, or None if there's no such nick
await c.request("LIST", until="323", collect="322")   # send a line, collect the replies
await c.request("WHO #x", until="315", key="#x")       # key: only replies whose 2nd param matches

ch = c["#python"]
ch.name, ch.topic, ch.joined, ch.is_query, len(ch)
ch.members                       # [Member(nick, modes, mark, user)], sorted like Textual's list
ch.nicks, "alice" in ch, ch.member("alice")
ch.say("hi"), ch.notice(...), ch.action("waves"), ch.command("/me waves"), ch.print("local")
ch.set_topic(...), ch.mode("+v", "alice"), ch.kick("spammer", "bye"), ch.part()
```

Every wrapper keeps the underlying Objective-C object in `.objc`, for anything not covered here. Use PyObjC naming: `c.objc.sendLine_("...")`.

### Events

```python
@irc.on("PRIVMSG", "NOTICE", client="libera", target="#python", match=r"^!(\w+)")
async def command(event):                  # plain functions work too
    event.command, event.params            # "PRIVMSG", ["#python", "!help me"]
    event.nick, event.hostmask, event.target, event.text, event.tags, event.time
    event.match.group(1)                   # set when you pass match=
    event.is_private, event.channel, event.ctcp
    event.reply("...")                     # replies in the channel, or privately for a PM

irc.handlers                     # registered handlers and filters, with call/error counts
irc.off(command)                 # or irc.off("__main__.command"); irc.clear() removes everything
irc.errors                       # recent handler failures

event = await irc.wait_for("JOIN", client="libera", timeout=30)

pending = irc.wait_for("PONG", timeout=10)     # registered immediately, so nothing is missed
c.send("PING :hello")
await pending

@irc.filter("PRIVMSG", match="buy now")        # runs synchronously, before Textual sees the line
def spam(event):
    return False                               # drop it; return a str to replace the raw line

irc.spawn(some_coroutine(), name="monitor")    # background task; re-spawning a name replaces it
```

- Commands are upper case. Numerics are three-digit strings like `"001"`, or ints like `1`. Give no commands, or `"*"`, to match everything.
- Registering a handler again under the same name replaces the old one. The default name is `module.qualname`, so you can edit and re-run a cell.
- Handlers and spawned tasks run on a dedicated asyncio loop. Don't block in plain function handlers, and use `async def` with `await asyncio.sleep()` instead of `time.sleep()`.
- If you register a handler from an SSH session, its `print()` output and tracebacks go to that session while it's connected, and to the log afterwards.
- Events are what the server sends. Your own outgoing messages only appear if the server echoes them (the `echo-message` capability).

### Startup scripts

Every `*.py` file in the plugin's `startup/` folder runs in the shared namespace when Textual launches, in alphabetical order. This is how automations persist. See [`examples/example_automations.py`](examples/example_automations.py).

### Threading

Textual changes its IRC state on the main thread, so the wrappers do every read and action there. The calling thread waits for the result. Filters are the exception: they run on Textual's connection thread while it waits. Inside a filter, reads happen inline and actions are queued rather than waited for, so keep filters quick.

Only one piece of code runs in the REPL at a time. If another session is busy, you'll see `[waiting for code running in another session to finish]`. Cells that use top-level `await` run on the IRC event loop, so tasks they create keep running after the cell finishes.

## Using it from an AI agent

The exec mode is designed for this: plain text output with no colours and no `Out[n]` prompts, and the exit status reports success. Give the agent something like:

> Textual's IRC state is reachable with `ssh -p 2323 127.0.0.1 '<python>'` (multi-line code can go on stdin). `irc` is the entry point: start with `irc.clients` and `help(irc)`. Top-level `await` works, e.g. `await irc.wait_for("PRIVMSG", target="#chan", timeout=60)`. Namespace state persists between calls.

The agent's SSH key must be in `authorized_keys`.

## Files

Settings and state live in Textual's sandbox container, at `~/Library/Group Containers/8482Q6EPL6.com.codeux.apps.textual/Library/Application Support/Textual/Python REPL/`. **Show Support Folder** in the preferences pane opens it.

| File | Purpose |
| --- | --- |
| `config.json` | `enabled`, `host`, `port`, `request_output` (`window`, `hidden` or `textual`). Easiest to edit in the preferences pane |
| `authorized_keys` | Public keys allowed to connect, re-read on every connection |
| `ssh_host_ed25519_key` | The REPL's SSH host key (generated) |
| `startup/` | Scripts run at launch |
| `python-repl.log` | Log, including `print()` output from code with no session attached |
| `ipython/` | IPython profile and history |

## Security

The REPL runs arbitrary Python as Textual. That code can send anything to any connected network and read Textual's sandbox container. It listens on `127.0.0.1` only and accepts public keys only, never passwords, so only add keys you trust. Listening on all interfaces exposes the REPL to your network, protected only by those keys. See [SECURITY.md](SECURITY.md) to report a vulnerability.

## Limitations

- Apple silicon only: the bundle ships an arm64 Python.
- The sandbox still applies. Code can only read files Textual can, and `!command` runs subprocesses inside the sandbox.
- Running code can't read stdin, so `input()`, interactive `pdb` and `%debug` don't work.
- Ctrl-C interrupts Python code, but a blocking C call such as `time.sleep()` isn't interrupted until it returns.
- Restarting the interpreter means restarting Textual.

## How it works

- `PythonREPL.bundle` is an ordinary Textual plugin. Its Objective-C principal class (`src/plugin/TPI_PythonREPL.m`) boots the embedded CPython and forwards a few plugin callbacks to a delegate written in Python. It also provides the preferences pane, which is built in code so the plugin builds without Xcode.
- [PyObjC](https://pyobjc.readthedocs.io) bridges Python to Textual's objects. [IPython](https://ipython.org) provides the shells, and [asyncssh](https://asyncssh.readthedocs.io) serves them.
- The tests drive the plugin inside a mock Textual (`tests/harness/Harness.m`), both normally and under the App Sandbox like the real thing.

See [CONTRIBUTING.md](CONTRIBUTING.md) to build, test and change it.

## License

BSD 3-Clause, the same licence as Textual. See [LICENSE](LICENSE). This project isn't affiliated with Codeux Software.
