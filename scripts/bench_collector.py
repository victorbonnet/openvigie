#!/usr/bin/env python3
"""Collector footprint benchmark (NFR: < 100 MB RSS, < 2% CPU idle on a Pi 5).

    uv run scripts/bench_collector.py generate <dir> [--sessions N] [--lines N]
    uv run scripts/bench_collector.py run <dir> [--max-rss-mb 100] [--idle-ticks 10]

``generate`` writes a synthetic home with Claude Code transcripts and Codex rollouts.
``run`` (a separate process, so RSS is the collector's alone) backfills all of it through the
real adapters and collector loop against a stub hub, then measures idle ticks. It prints a JSON
report and exits 1 if peak RSS exceeds the budget.
"""

from __future__ import annotations

import argparse
import json
import resource
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from openvigie.adapters.claude_code import ClaudeCodeAdapter
from openvigie.adapters.codex import CodexAdapter
from openvigie.adapters.registry import Discovery
from openvigie.collector.client import HubClient
from openvigie.collector.daemon import Collector
from openvigie.collector.host import build_context
from openvigie.collector.state import StateStore
from openvigie.config import CollectorSettings

FILLER = "lorem ipsum dolor sit amet " * 12


def generate(root: Path, sessions: int, lines: int) -> None:
    now = datetime.now(UTC)
    project = root / ".claude" / "projects" / "-home-bench-src"
    project.mkdir(parents=True, exist_ok=True)
    for s in range(sessions):
        sid = f"bench-{s:05d}"
        with (project / f"{sid}.jsonl").open("w") as fh:
            for i in range(lines):
                when = (now - timedelta(minutes=sessions * lines - s * lines - i)).isoformat()
                if i % 2:
                    entry: dict[str, Any] = {
                        "type": "user",
                        "sessionId": sid,
                        "timestamp": when,
                        "message": {"role": "user", "content": FILLER},
                    }
                else:
                    entry = {
                        "type": "assistant",
                        "sessionId": sid,
                        "timestamp": when,
                        "requestId": f"req-{s}-{i}",
                        "version": "2.1.299",
                        "message": {
                            "id": f"msg-{s}-{i}",
                            "model": "claude-opus-5-5",
                            "content": [{"type": "text", "text": FILLER}],
                            "usage": {
                                "input_tokens": 12,
                                "output_tokens": 340,
                                "cache_read_input_tokens": 40000,
                                "cache_creation_input_tokens": 2000,
                            },
                        },
                    }
                fh.write(json.dumps(entry) + "\n")
    day = root / ".codex" / "sessions" / now.strftime("%Y/%m/%d")
    day.mkdir(parents=True, exist_ok=True)
    for s in range(sessions):
        with (day / f"rollout-bench-{s:05d}.jsonl").open("w") as fh:
            fh.write(
                json.dumps(
                    {
                        "timestamp": now.isoformat(),
                        "type": "session_meta",
                        "payload": {"id": f"cx-{s}", "cli_version": "0.46.0"},
                    }
                )
                + "\n"
            )
            fh.write(
                json.dumps(
                    {
                        "timestamp": now.isoformat(),
                        "type": "turn_context",
                        "payload": {"model": "gpt-5-codex"},
                    }
                )
                + "\n"
            )
            for i in range(lines // 2):
                total = {
                    "input_tokens": 1000 * (i + 1),
                    "cached_input_tokens": 500 * (i + 1),
                    "output_tokens": 50 * (i + 1),
                    "reasoning_output_tokens": 10 * (i + 1),
                }
                fh.write(
                    json.dumps(
                        {
                            "timestamp": now.isoformat(),
                            "type": "response_item",
                            "payload": {"type": "message", "content": FILLER},
                        }
                    )
                    + "\n"
                )
                fh.write(
                    json.dumps(
                        {
                            "timestamp": now.isoformat(),
                            "type": "event_msg",
                            "payload": {
                                "type": "token_count",
                                "info": {"total_token_usage": total},
                            },
                        }
                    )
                    + "\n"
                )
    size = sum(p.stat().st_size for p in root.rglob("*.jsonl"))
    print(f"generated {size / 1e6:.0f} MB in {root}")


def peak_rss_mb() -> float:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak / 1e6 if sys.platform == "darwin" else peak / 1024


def cpu_s() -> float:
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return usage.ru_utime + usage.ru_stime


def run(root: Path, max_rss_mb: float, idle_ticks: int) -> int:
    pushed = {"usage": 0}

    def stub_hub(method: str, url: str, body: bytes | None, headers: Any, timeout: float) -> Any:
        payload = json.loads(body or b"{}")
        pushed["usage"] += len(payload.get("usage", []))
        return 200, b'{"ignored": []}'

    settings = CollectorSettings(homes=(root,), slow_tick_s=0.0, backfill_days=3650)
    collector = Collector(
        settings,
        build_context(settings, root / "state"),
        Discovery(adapters=[ClaudeCodeAdapter(), CodexAdapter()]),
        HubClient("http://127.0.0.1:7878", token="ovh_bench", transport=stub_hub),  # noqa: S106
        store=StateStore(root / "state" / "collector.db"),
    )
    started, cpu0 = time.perf_counter(), cpu_s()
    backfill_ticks = 0
    while True:
        before = pushed["usage"]
        collector.tick()
        backfill_ticks += 1
        if pushed["usage"] == before and backfill_ticks > 1:
            break
    backfill_s, backfill_cpu = time.perf_counter() - started, cpu_s() - cpu0
    idle_cpu0 = cpu_s()
    for _ in range(idle_ticks):
        collector.tick()
    per_tick = (cpu_s() - idle_cpu0) / idle_ticks
    report = {
        "usage_records": pushed["usage"],
        "backfill_ticks": backfill_ticks,
        "backfill_wall_s": round(backfill_s, 2),
        "backfill_cpu_s": round(backfill_cpu, 2),
        "idle_cpu_ms_per_tick": round(per_tick * 1000, 1),
        "idle_cpu_percent_at_5s_tick": round(per_tick / 5.0 * 100, 2),
        "peak_rss_mb": round(peak_rss_mb(), 1),
        "budget_rss_mb": max_rss_mb,
    }
    print(json.dumps(report, indent=2))
    if report["peak_rss_mb"] > max_rss_mb:
        print(f"FAIL: peak RSS {report['peak_rss_mb']} MB > {max_rss_mb} MB", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    gen = sub.add_parser("generate")
    gen.add_argument("dir", type=Path)
    gen.add_argument("--sessions", type=int, default=200)
    gen.add_argument("--lines", type=int, default=1000)
    bench = sub.add_parser("run")
    bench.add_argument("dir", type=Path)
    bench.add_argument("--max-rss-mb", type=float, default=100.0)
    bench.add_argument("--idle-ticks", type=int, default=10)
    args = parser.parse_args(argv)
    if args.cmd == "generate":
        generate(args.dir, args.sessions, args.lines)
        return 0
    return run(args.dir, args.max_rss_mb, args.idle_ticks)


if __name__ == "__main__":
    sys.exit(main())
