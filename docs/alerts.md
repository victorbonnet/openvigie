# Budgets and alerts

openvigie can tell you when agents spend more than you planned (FR-14). Budgets live in a file,
the hub checks them every minute, and each crossing becomes an alert. Budgets only alert: they
never stop an agent.

## Budgets

Put them in `~/.config/openvigie/budgets.toml` on the hub machine:

```toml
[[budgets]]
id = "claude-daily"
agent_type = "claude-code"  # or host = "pi5", or agent_id = "…"; none: every agent
period = "day"              # "day" or "month", in the hub's display_timezone
max_cost_usd = 5
max_tokens = 2000000        # optional; set one limit or both
warn_at = 0.8               # also alert at 80 % (the default)

[[budgets]]
id = "everything-monthly"
period = "month"
max_cost_usd = 150
```

The *Budgets* page shows each budget's use in its current period. If a file has a mistake, the
page names the key and the hub ignores the file until it's fixed.

- **Periods** start at local midnight, or on the first of the month, in `display_timezone`.
- **Unknown ≠ zero.** If some usage has no known price, the cost shown is a lower bound
  (`≥ $4.20`). It can still trigger an alert.

## Alerts

An alert is raised once per budget, period and level: when a budget reaches `warn_at`, and
again when it goes over its limit. The *Budgets* link in the menu counts the alerts from the
last 24 hours, and the page lists recent ones. Alerts are kept.
