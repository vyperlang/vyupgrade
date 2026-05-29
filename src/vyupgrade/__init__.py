"""Vyper source upgrader."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("vyupgrade")
except PackageNotFoundError:
    __version__ = "0.0.0"
