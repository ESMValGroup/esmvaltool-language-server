"""Language server for ESMValTool recipes."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("esmvaltool-language-server")
except PackageNotFoundError:  # pragma: no cover
    __version__ = "unknown"
