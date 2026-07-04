"""alle — multi-location VPN gateways via one sing-box process (WireGuard)."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("alle-proxy")
except PackageNotFoundError:
    __version__ = "0.0.0+unknown"
