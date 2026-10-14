"""Command-line entry point."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import timedelta

from openvigie import __version__
from openvigie.redact import install_log_redaction


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
        force=True,
    )
    install_log_redaction()


def cmd_scan(args: argparse.Namespace) -> int:
    from openvigie.adapters.registry import discover
    from openvigie.collector.host import build_context
    from openvigie.collector.scan import run_scan, to_json, to_text
    from openvigie.config import ConfigError, load_collector_settings

    try:
        settings = load_collector_settings()
    except ConfigError as exc:
        print(f"openvigie: {exc}", file=sys.stderr)
        return 2
    discovery = discover(settings.enable_plugins, settings.disable_adapters)
    ctx = build_context(settings)
    result = run_scan(
        ctx, discovery, timedelta(seconds=settings.active_window_s), only=args.adapter
    )
    print(to_json(result) if args.json else to_text(result))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="openvigie", description="Control tower for AI agents on your own machines."
    )
    parser.add_argument("--version", action="version", version=f"openvigie {__version__}")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-v", "--verbose", action="store_true", help="debug logging to stderr")
    sub = parser.add_subparsers(dest="command")

    scan = sub.add_parser(
        "scan", parents=[common], help="list the agents openvigie finds on this machine"
    )
    scan.add_argument("--json", action="store_true", help="machine-readable output")
    scan.add_argument("--adapter", metavar="TYPE", help="only run one adapter, e.g. codex")
    scan.set_defaults(func=cmd_scan)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    setup_logging(args.verbose)
    return int(args.func(args))
