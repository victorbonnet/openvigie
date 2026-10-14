"""Command-line entry point. Subcommands (collect, hub, enrol) land in later PRs."""

import argparse

from openvigie import __version__


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="openvigie")
    parser.add_argument("--version", action="version", version=f"openvigie {__version__}")
    parser.parse_args(argv)
    parser.print_help()
    return 0
