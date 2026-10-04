# Security policy

This plugin runs arbitrary Python inside Textual for anyone who can authenticate to its SSH server, so its
authentication and network exposure matter.

By default the server listens on `127.0.0.1` only and accepts public keys listed in the plugin's
`authorized_keys`. It never accepts passwords. Keep the key list short, and only listen on other interfaces if you
understand the risk.

## Reporting a vulnerability

Please report vulnerabilities privately with
[GitHub's private vulnerability reporting](https://github.com/predakanga/textual-python-repl/security/advisories/new)
rather than in a public issue. Include steps to reproduce and the versions of the plugin, Textual and macOS.
