"""Command-line entry point."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import timedelta
from pathlib import Path

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


def cmd_hub_serve(args: argparse.Namespace) -> int:
    import dataclasses

    from pydantic import ValidationError

    from openvigie.config import ConfigError, describe, load_hub_settings

    try:
        settings = load_hub_settings()
        overrides = {k: v for k, v in (("bind", args.bind), ("port", args.port)) if v is not None}
        settings = dataclasses.replace(settings, **overrides)  # validates again
    except ConfigError as exc:
        print(f"openvigie: {exc}", file=sys.stderr)
        return 2
    except ValidationError as exc:
        print(f"openvigie: {describe(exc, 'hub')}", file=sys.stderr)
        return 2
    from openvigie.hub.server import serve

    serve(settings)
    return 0


def parse_duration(text: str) -> timedelta:
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if not text or text[-1] not in units or not text[:-1].isdigit():
        raise argparse.ArgumentTypeError("use a number with s, m, h or d, e.g. 15m")
    return timedelta(seconds=int(text[:-1]) * units[text[-1]])


def cmd_hub_enrol_code(args: argparse.Namespace) -> int:
    from datetime import UTC, datetime

    from openvigie.config import ConfigError, load_hub_settings
    from openvigie.hub.store.credentials import Credentials
    from openvigie.hub.store.db import Database

    try:
        settings = load_hub_settings()
    except ConfigError as exc:
        print(f"openvigie: {exc}", file=sys.stderr)
        return 2
    db = Database(settings.resolved_db_path())
    try:
        code = Credentials(db).create_enrol_code(datetime.now(UTC), args.ttl)
    finally:
        db.close()
    minutes = int(args.ttl.total_seconds() // 60)
    print(f"Enrolment code (single use, valid {minutes} min):\n\n  {code}\n")
    print("On the host to enrol, run:")
    print(f"  openvigie enrol http://127.0.0.1:{settings.port} {code}   # same machine")
    print(f"  openvigie enrol https://<hub>.<tailnet>.ts.net {code}    # over Tailscale")
    return 0


def cmd_hub_audit(args: argparse.Namespace) -> int:
    from openvigie.config import ConfigError, load_hub_settings
    from openvigie.hub.audit_cli import run

    try:
        settings = load_hub_settings()
    except ConfigError as exc:
        print(f"openvigie: {exc}", file=sys.stderr)
        return 2
    return run(settings, args.audit_command)


def cmd_hub_hosts(args: argparse.Namespace) -> int:
    from openvigie.config import ConfigError, load_hub_settings
    from openvigie.hub.hosts_cli import run

    try:
        settings = load_hub_settings()
    except ConfigError as exc:
        print(f"openvigie: {exc}", file=sys.stderr)
        return 2
    return run(settings, args.hosts_command, getattr(args, "host", None), args.ttl)


def cmd_enrol(args: argparse.Namespace) -> int:
    from openvigie.collector.client import HubError
    from openvigie.collector.enrol import enrol
    from openvigie.collector.probes import load_host
    from openvigie.config import ConfigError, load_collector_settings, state_dir

    try:
        settings = load_collector_settings()
        token_file = enrol(args.hub_url, args.code, load_host(state_dir()), settings)
    except (ConfigError, HubError, ValueError) as exc:
        print(f"openvigie: enrolment failed: {exc}", file=sys.stderr)
        return 1
    print(f"Enrolled. Token saved to {token_file} (mode 0600). Start `openvigie collect`.")
    return 0


def cmd_collect(args: argparse.Namespace) -> int:
    import signal
    import threading

    from openvigie.adapters.registry import discover
    from openvigie.collector.client import HubClient
    from openvigie.collector.daemon import Collector
    from openvigie.collector.host import build_context
    from openvigie.config import ConfigError, load_collector_settings, read_private_file

    try:
        settings = load_collector_settings()
        if not settings.hub_url:
            raise ConfigError("no hub configured; run `openvigie enrol <hub-url> <code>` first")
        token = read_private_file(settings.resolved_token_file())
        client = HubClient(settings.hub_url, token=token)
    except FileNotFoundError:
        print("openvigie: no token found; run `openvigie enrol <hub-url> <code>`", file=sys.stderr)
        return 2
    except (ConfigError, ValueError) as exc:
        print(f"openvigie: {exc}", file=sys.stderr)
        return 2
    from openvigie.collector.state import StateStore
    from openvigie.config import state_dir

    discovery = discover(settings.enable_plugins, settings.disable_adapters)
    store = StateStore(state_dir() / "collector.db", settings.spool_limit_mb * 1024 * 1024)
    otlp = None
    if settings.otlp.enabled:
        from openvigie.collector.otlp import OtlpReceiver

        try:
            otlp_token = (
                read_private_file(settings.otlp.token_file) if settings.otlp.token_file else None
            )
            otlp = OtlpReceiver(settings.otlp.port, otlp_token)
            otlp.start()
        except (ConfigError, OSError) as exc:
            print(f"openvigie: OTLP receiver: {exc}", file=sys.stderr)
            return 2
    from openvigie.openshell.cli import Reader, cli_runner

    # OpenShell sandboxes are Linux-only (D11)
    openshell = Reader(cli_runner()) if sys.platform.startswith("linux") else None
    collector = Collector(settings, build_context(settings), discovery, client, store=store,
                          otlp=otlp, openshell=openshell)  # fmt: skip
    if args.once:
        return 0 if collector.tick() else 1
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    print(f"openvigie collector pushing to {client.base_url} every {settings.fast_tick_s:g}s")
    collector.run(stop)
    return 0


def cmd_service_print(args: argparse.Namespace) -> int:
    from openvigie.service import render

    try:
        print(render(args.role, args.kind, [Path(h) for h in args.home]), end="")
    except ValueError as exc:
        print(f"openvigie: {exc}", file=sys.stderr)
        return 2
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

    hub = sub.add_parser("hub", help="run and manage the hub (store + web UI)")
    hub_sub = hub.add_subparsers(dest="hub_command", required=True)
    serve = hub_sub.add_parser("serve", parents=[common], help="serve the web UI and API")
    serve.add_argument("--bind", help="address to bind (default 127.0.0.1)")
    serve.add_argument("--port", type=int, help="port (default 7878)")
    serve.set_defaults(func=cmd_hub_serve)
    code = hub_sub.add_parser(
        "enrol-code", parents=[common], help="create a one-time code to enrol a collector"
    )
    code.add_argument("--ttl", type=parse_duration, default=timedelta(minutes=15))
    code.set_defaults(func=cmd_hub_enrol_code)
    hosts = hub_sub.add_parser("hosts", help="list, revoke or re-enrol collector hosts")
    hosts_sub = hosts.add_subparsers(dest="hosts_command", required=True)
    hosts_sub.add_parser("list", parents=[common], help="enrolled hosts and their tokens")
    revoke = hosts_sub.add_parser("revoke", parents=[common], help="cut a host off now")
    revoke.add_argument("host", help="host id, id prefix or hostname")
    rotate = hosts_sub.add_parser(
        "rotate", parents=[common], help="one-time code to re-enrol a host, keeping its history"
    )
    rotate.add_argument("host", help="host id, id prefix or hostname")
    hosts.set_defaults(func=cmd_hub_hosts, ttl=timedelta(minutes=15))
    audit = hub_sub.add_parser("audit", help="check or export the audit log of admin actions")
    audit_sub = audit.add_subparsers(dest="audit_command", required=True)
    audit_sub.add_parser("verify", parents=[common], help="check the hash chain")
    audit_sub.add_parser("export", parents=[common], help="print every entry as JSON lines")
    audit.set_defaults(func=cmd_hub_audit)

    enrol = sub.add_parser("enrol", parents=[common], help="enrol this host with a hub")
    enrol.add_argument("hub_url", help="e.g. http://127.0.0.1:7878 or https://hub.tailnet.ts.net")
    enrol.add_argument("code", help="one-time code from `openvigie hub enrol-code`")
    enrol.set_defaults(func=cmd_enrol)

    service = sub.add_parser("service", help="print service definitions (installs nothing)")
    service_sub = service.add_subparsers(dest="service_command", required=True)
    printer = service_sub.add_parser(
        "print",
        parents=[common],
        help="print a systemd unit or launchd plist to review and install yourself",
    )
    printer.add_argument("role", choices=["collector", "hub"])
    kinds = printer.add_mutually_exclusive_group()
    kinds.add_argument(
        "--systemd",
        dest="kind",
        action="store_const",
        const="system",
        help="Linux system service with a dedicated user (default)",
    )
    kinds.add_argument(
        "--systemd-user",
        dest="kind",
        action="store_const",
        const="user",
        help="Linux per-user service (weaker isolation)",
    )
    kinds.add_argument(
        "--launchd", dest="kind", action="store_const", const="launchd", help="macOS LaunchAgent"
    )
    printer.add_argument(
        "--home",
        action="append",
        default=[],
        metavar="DIR",
        help="home folder whose agent folders the system collector may read",
    )
    printer.set_defaults(func=cmd_service_print, kind="system")

    collect = sub.add_parser("collect", parents=[common], help="run the collector daemon")
    collect.add_argument("--once", action="store_true", help="one tick and push, then exit")
    collect.set_defaults(func=cmd_collect)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    setup_logging(args.verbose)
    return int(args.func(args))
