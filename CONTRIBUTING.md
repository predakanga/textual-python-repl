# Contributing

Thanks for helping! Bug reports, ideas and pull requests are all welcome.

## Setup

You need an Apple silicon Mac with the Xcode command line tools. Linting runs a pinned [ruff](https://docs.astral.sh/ruff/) through [uv](https://docs.astral.sh/uv/) (`uvx`), and regenerating the dependency lock needs uv too.

## Make targets

| Target | What it does |
| --- | --- |
| `make` | Build `build/PythonREPL.bundle` |
| `make test` | Run the test suite against a mock Textual (`V=1` for verbose output) |
| `make test-sandbox` | The same suite with the mock running under the App Sandbox, like Textual |
| `make lint` | `ruff check` and `ruff format --check` |
| `make check` | `lint` and `test` |
| `make install` | Hand the bundle to Textual's extension installer (`open -a Textual`), then restart Textual |
| `make dist` | Zip the bundle for a release |
| `make lock` | Regenerate `requirements.txt` from `requirements.in` |
| `make clean` / `make distclean` | Remove `build/`, and with `distclean` the download cache too |

To bump Python, update `PYTHON_VERSION`, `PBS_RELEASE` and `PBS_SHA256` in the Makefile, using the release's `SHA256SUMS` and the `install_only_stripped` build. Then run `make lock`.

## Layout

```
src/plugin/          Objective-C: the principal class and the preferences pane
src/textual_repl/    Python: everything else
  plugin.py            bootstrap, the delegate the Objective-C side calls, /py, preferences
  irc.py               the irc API: wrappers, events, the event bus
  shell.py             IPython: shared namespace, exec and interactive shells
  sshserver.py         asyncssh front end
  windows.py           REPL windows
  mainthread.py        running things on Textual's main thread
  output.py            per-session stdout/stderr routing
tests/               unittest suite; tests/harness/Harness.m is the mock Textual
```

## Testing

The tests never touch a real Textual. `tests/harness/Harness.m` loads the bundle exactly as Textual's plugin manager does, with just enough mock IRC objects. It echoes everything "Textual" would do to stdout, so the tests in `tests/` can make assertions about it over SSH. The mocks also complain (with lines starting `!!`) if Textual objects are touched off the main thread.

You can also run the harness by hand:

```sh
make build/tests/Harness
mkdir -p /tmp/repl/"Python REPL"
echo '{"port": 2424}' > /tmp/repl/"Python REPL"/config.json
cp ~/.ssh/id_ed25519.pub /tmp/repl/"Python REPL"/authorized_keys
build/tests/Harness build/PythonREPL.bundle /tmp/repl
```

Each line on its stdin is one of:

- a raw server line to inject, such as `:nick!u@h PRIVMSG #python :hi`;
- `/py …` or `/pyrepl`;
- `!snapshot <file.png>`, which renders the preferences pane (put `dark` in the name for dark mode).

## Things to know

- **Only Textual can write to its container.** macOS's app container protection makes writes into Textual's group container fail with EINTR ("Interrupted system call") for every other process, including `cp`, `rsync` and Installer.app. That's why installing goes through Textual's own importer, and why settings are changed through the preferences pane.
- **Installed files are always quarantined.** Files a sandboxed app writes get a `com.apple.quarantine` flag, so Gatekeeper checks the plugin when Textual first loads it. Without Developer ID signing and notarization, users approve it once per installed version (see the README).
- **The plugin is a guest in Textual's process.** Exceptions must never escape into Objective-C. Every delegate method and every block run on the main thread catches everything. The plugin doesn't touch process-wide state such as signal handlers.
- **Check Textual's headers for types.** For example, `IRCRemoteCommand` is an `NSUInteger` enum, not a string. Getting a type like that wrong crashes Textual, and the mocks only catch it if they make the same call Textual does.
- The build ships unchecked-hash `.pyc` files, because the plugin never writes bytecode into its signed bundle. If you copy edited sources into a built bundle by hand, delete its `__pycache__` too, or the old code keeps running. `make` does this for you.

## Pull requests

- Run `make check`, and `make test-sandbox` if you touched anything near the sandbox, threads or sockets.
- Add a test for bug fixes where you can, and check that it fails without the fix.
- Add an entry under "Unreleased" in [CHANGELOG.md](CHANGELOG.md).

## Releasing

1. Update `VERSION` and move the "Unreleased" changelog entries under the new version.
2. Commit, tag (`git tag v0.2.0`) and push the tag. The release workflow builds, tests, zips and publishes a GitHub release.

If the repository has the signing secrets described in `.github/workflows/release.yml`, the release is signed with a Developer ID and notarized. Otherwise it's ad-hoc signed.
