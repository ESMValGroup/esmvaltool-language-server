"""Start the language server."""

from __future__ import annotations

import argparse
import logging

from esmvaltool_language_server import __version__
from esmvaltool_language_server.server import server


def main() -> None:
    """Start the language server, communicating over stdin and stdout."""
    parser = argparse.ArgumentParser(
        prog="esmvaltool-language-server",
        description=(
            "Language server for ESMValTool recipes. Editors start it and "
            "communicate with it over stdin and stdout."
        ),
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument(
        "--stdio",
        action="store_true",
        help="Communicate over stdin and stdout (the default).",
    )
    parser.parse_args()
    # stdout is used for the protocol, so log to stderr.
    logging.basicConfig(level=logging.INFO)

    server.start_io()


if __name__ == "__main__":
    main()
