# Collector footprint

Budget (PRD, non-functional requirements): the collector stays **under 100 MB of RAM** and
**under 2% CPU at idle on a Raspberry Pi 5**.

## How it is measured

`scripts/bench_collector.py` generates a synthetic home (Claude Code transcripts and Codex
rollouts), then runs the real adapters and collector loop against a stub hub in a separate
process, so the peak RSS is the collector's alone:

```sh
uv run scripts/bench_collector.py generate /tmp/bench --sessions 200 --lines 1000   # ~186 MB
uv run scripts/bench_collector.py run /tmp/bench --max-rss-mb 100
rm -rf /tmp/bench/state
uv run scripts/bench_collector.py run /tmp/bench --max-rss-mb 100 --traces   # trace export on
```

It reports the backfill time, CPU per idle tick, idle CPU as a percentage of the default 5 s
tick, and peak RSS, and exits 1 if RSS exceeds the budget. CI runs a half-size version on
`ubuntu-24.04-arm` on every PR (the `perf` job), with and without traces.

With trace export on, every model call and tool call also becomes a span, which costs far more
memory per transcript line than a usage record. Adapters then read 4 MiB per tick instead of
16 MiB, so a large backfill takes more ticks but the peak stays low.

While a backfill takes more than one tick, the Usage and agent pages say the agent's history is
still being read and mark its totals with ≥. Once the budget of a tick is spent, the files left
are only `stat`-ed to measure what remains, as on any idle tick.

## Results

| Machine | Data | Peak RSS | Idle CPU per tick | Idle CPU at 5 s tick | Backfill |
| --- | --- | --- | --- | --- | --- |
| x86_64 cloud VM (dev) | 186 MB, 200k usage records | 68.5 MB | 21 ms | 0.42% | 16 s |
| x86_64 cloud VM (dev), `--traces` | same, + 200k spans | 57.9 MB | 22 ms | 0.44% | 40 s (26 ticks) |
| Raspberry Pi 5 | _to measure_ | | | | |
| Apple Silicon Mac | _to measure_ | | | | |

Idle cost is dominated by the process snapshot and one `stat` per transcript file each tick;
it grows with the number of session files. If a Pi measurement lands near the 2% budget, raise
`fast_tick_s` in `collector.toml` (status freshness stays within 10 s up to `fast_tick_s = 8`).

## Measuring on a Pi or a Mac

Run the two commands above on the device and add a row to the table. For a long-running
check, start `openvigie collect -v` and watch `ps -o rss,pcpu -p <pid>` for a few minutes.
