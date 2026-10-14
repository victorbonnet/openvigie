#!/usr/bin/env bash
# Print the OpenShell outputs openvigie parses, between "=== <file> ===" markers, so CI logs (or
# a maintainer) can turn them into fixtures under tests/openshell/fixtures/v<version>/.
# Read-only except for one throwaway sandbox it creates and deletes. Usage: record_openshell.sh
set -u
NAME=ov-fixture
section() { printf '\n=== %s ===\n' "$1"; }
run() { local file=$1; shift; section "$file"; "$@" 2>&1; printf '\n--- exit %s ---\n' "$?"; }

run version.txt openshell --version
run status.txt openshell status
for cmd in "sandbox create" "sandbox list" "sandbox get" "policy get" "policy list" \
           "policy update" "policy set"; do
  # shellcheck disable=SC2086
  run "help-${cmd// /-}.txt" openshell $cmd --help
done
# create may stay attached to its command: run it in the background and wait for the sandbox
section create.json
( timeout 300 openshell sandbox create --name "$NAME" --label openvigie.agent=claude-code \
    --output json -- sleep 3600 > /tmp/ov-create.out 2>&1; echo "--- exit $? ---" >> /tmp/ov-create.out ) &
for _ in $(seq 60); do
  openshell sandbox get "$NAME" --output json > /dev/null 2>&1 && break
  if grep -q -- '--- exit [1-9]' /tmp/ov-create.out 2>/dev/null; then
    cat /tmp/ov-create.out; section create-nolabel.json
    ( timeout 300 openshell sandbox create --name "$NAME" --output json -- sleep 3600 \
        > /tmp/ov-create.out 2>&1; echo "--- exit $? ---" >> /tmp/ov-create.out ) &
  fi
  sleep 5
done
sleep 20 # let the policy load
cat /tmp/ov-create.out
run sandbox-list.json openshell sandbox list -o json
run sandbox-get.json openshell sandbox get "$NAME" --output json
run policy-base.json openshell policy get "$NAME" --base --output json
run policy-full.json openshell policy get "$NAME" --full --output json
run policy-list.txt openshell policy list "$NAME"
run policy-only.yaml openshell sandbox get "$NAME" --policy-only
run update-dry-run.txt openshell policy update "$NAME" --rule-name ov_probe \
  --binary /usr/bin/curl --add-endpoint api.github.com:443:read-only:rest:enforce --dry-run
run sandbox-get-missing.json openshell sandbox get no-such-sandbox --output json

# Host side, for mapping agents to sandboxes (#149)
run docker-ps.json docker ps --no-trunc --format json
run ps.txt ps -eo pid,ppid,user,comm,args --forest
main=$(pgrep -f 'sleep 3600' | head -1)
if [ -n "$main" ]; then
  run main-cgroup.txt cat "/proc/$main/cgroup"
  run main-ancestry.txt sh -c "p=$main; while [ \"\$p\" -gt 1 ]; do ps -o pid=,ppid=,comm= -p \$p; p=\$(ps -o ppid= -p \$p | tr -d ' '); done"
fi

run delete.txt openshell sandbox delete "$NAME"
run status-gateway-down.txt sh -c 'systemctl --user stop openshell-gateway && openshell status; openshell sandbox list -o json'
exit 0
