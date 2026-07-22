"""CLI: `plane-pages-mcp serve` (default) and `plane-pages-mcp verify`."""

from __future__ import annotations

import argparse
import sys

from .config import Config, ConfigError


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="plane-pages-mcp")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="run the MCP server (default)")
    sub.add_parser("verify", help="Phase 0 runtime checks against DB + live service")
    args = parser.parse_args(argv)

    try:
        cfg = Config.from_env()
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        sys.exit(2)

    if args.command == "verify":
        from . import verify

        verify.main(cfg)
    else:
        from . import server

        server.run(cfg)


if __name__ == "__main__":
    main()
