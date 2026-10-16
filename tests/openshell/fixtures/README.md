# OpenShell fixtures

Real outputs of the `openshell` CLI, recorded on a GitHub ARM runner by
`.github/workflows/openshell-fixtures.yml` (PRD decision D13). One folder per supported version.

## Re-record (for example after bumping `MIN_SUPPORTED`)

1. Run the **openshell-fixtures** workflow (Actions → Run workflow).
2. Download the `record` job log.
3. Run `python scripts/extract_openshell_fixtures.py <job-log.txt>`. This writes
   `tests/openshell/fixtures/v<version>/` and replaces the host key fingerprint and gateway token
   id with placeholders.
4. Commit the result.

## What is in a folder

- **Sandbox `ov-fixture`:** the default policy, with no network rules and the label
  `openvigie.agent=claude-code`.
  - `sandbox-list.json`, `sandbox-list-all.json`, `sandbox-get.json`;
  - `policy-base.json`, `policy-full.json`, `policy-list.txt`, `policy-only.yaml`,
    `update-dry-run.txt`.
- **Sandbox `ov-net`:** created with two network rules, then updated live.
  - `net-sandbox-get.json`, `net-policy-base.json` (before the update);
  - `net-policy-update.txt`, `net-policy-list.txt`, `net-policy-base-v2.json` (after it).
- **Mapping agents to sandboxes (#149).** The main process of `ov-fixture` is `sleep 3600`, a
  child of `openshell-sandbox`.
  - `main-cgroup.txt`, `main-ancestry.txt`, `ps.txt`;
  - `main-stat.txt`, `proc-btime.txt` and `clk-tck.txt` give its start time, which matches
    `main_process_started_at_ms` in `sandbox-get.json` to within 0.1 s;
  - `docker-ps.json` is for reference only: the collector never talks to Docker.
  - Because CI logs are public, `ps.txt` stops each command line at the executable and
    `docker-ps.json` keeps only `openshell.ai/*` labels and each container's executable;
    container env is never recorded. Docker joins labels with commas, so a label value that
    itself contains a comma is cut there.
- **Errors and help:**
  - `sandbox-get-missing.json` (no such sandbox), `status-gateway-down.txt` (gateway stopped);
  - `status.txt`, `version.txt`, and `help-*.txt` for each command openvigie may call.

A file starting with `# exit N` recorded a command that exited with status N.
Commands that create a sandbox print a progress line on stderr; openvigie parses stdout only.
