"""Python REPL for Textual.

Embeds IPython inside Textual and serves it over SSH. See the README,
``help(irc)`` for the IRC API, and ``textual_repl.irc`` for the wrappers.
"""

try:
    from ._version import __version__  # written by the build
except ImportError:  # running from a source checkout
    __version__ = "0+unknown"
