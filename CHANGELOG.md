# Changelog

All notable changes are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.0] - 2026-10-04

First public release.

### Added

- IPython embedded in Textual, served over SSH with public key authentication: interactive sessions, plus one-off
  `ssh host 'code'` exec requests for scripts and AI agents.
- A Pythonic API over Textual's live IRC state (`irc.clients`, channels, members, sending), with event handlers,
  synchronous filters, `wait_for`, request/response helpers such as `whois()`, and background tasks.
- `/py` and `/pyrepl` commands in Textual's input field.
- Startup scripts, run when Textual launches.
- A preferences pane: SSH server on/off, address and port, live status, authorized keys, and where request replies go.
- Replies to requests made from Python go to per-session "Python REPL" windows instead of the active window.
- `make install`, which installs through Textual's own extension importer, and zipped release builds.

[Unreleased]: https://github.com/predakanga/textual-python-repl/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/predakanga/textual-python-repl/releases/tag/v0.1.0
