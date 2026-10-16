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
# --detach: structured output can't be combined with an attached command
run create.json openshell sandbox create --name "$NAME" --label openvigie.agent=claude-code \
  --detach --output json -- sleep 3600
for _ in $(seq 60); do  # wait until Ready (image pull, policy load)
  openshell sandbox get "$NAME" --output json 2>/dev/null | grep -q '"Ready"' && break
  sleep 5
done
sleep 10
run sandbox-list.json openshell sandbox list -o json
run sandbox-list-all.json openshell sandbox list --all-workspaces -o json
run sandbox-get.json openshell sandbox get "$NAME" --output json
run policy-base.json openshell policy get "$NAME" --base --output json
run policy-full.json openshell policy get "$NAME" --full --output json
run policy-list.txt openshell policy list "$NAME"
run policy-only.yaml openshell sandbox get "$NAME" --policy-only
run update-dry-run.txt openshell policy update "$NAME" --rule-name ov_probe \
  --binary /usr/bin/curl --add-endpoint api.github.com:443:read-only:rest:enforce --dry-run
run sandbox-get-missing.json openshell sandbox get no-such-sandbox --output json

# Host side, for mapping agents to sandboxes (#149). CI logs are public, so print only what the
# mapping needs: no `docker inspect` (it dumps container env), only openshell.ai/* labels, and
# each command line cut after the executable (arguments can carry secrets).
docker_ps() {
  docker ps --no-trunc --format json | jq -c \
    '.Labels |= (split(",") | map(select(startswith("openshell.ai/"))) | join(","))'
}
ps_tree() {
  # --forest marks (\_ and |) prefix both comm and args: keep words up to argv[0]
  ps -eo pid,ppid,user,comm,args --forest | awk '{
    out = $1 " " $2 " " $3; seen = 0
    for (i = 4; i <= NF && seen < 2; i++) { out = out " " $i; if ($i != "\\_" && $i != "|") seen++ }
    print out
  }'
}
run docker-ps.json docker_ps
run ps.txt ps_tree
main=$(pgrep -f 'sleep 3600' | head -1)
if [ -n "$main" ]; then
  run main-cgroup.txt cat "/proc/$main/cgroup"
  run main-stat.txt cat "/proc/$main/stat"
  run proc-btime.txt grep btime /proc/stat
  run clk-tck.txt getconf CLK_TCK
  run main-ancestry.txt sh -c "p=$main; while [ \"\$p\" -gt 1 ]; do ps -o pid=,ppid=,comm= -p \$p; p=\$(ps -o ppid= -p \$p | tr -d ' '); done"
fi

# A second sandbox whose policy has network rules, for the network-rule parsing
cat > /tmp/ov-policy.yaml <<'POLICY'
version: 1
filesystem_policy:
  include_workdir: true
  read_only: [/usr, /lib, /etc]
  read_write: [/tmp]
landlock:
  compatibility: best_effort
network_policies:
  github_readonly:
    endpoints:
      - host: api.github.com
        port: 443
        protocol: rest
        enforcement: enforce
        access: read-only
    binaries:
      - path: /usr/bin/curl
  pypi:
    endpoints:
      - host: pypi.org
        port: 443
      - host: files.pythonhosted.org
        port: 443
    binaries:
      - path: /usr/bin/python3*
POLICY
run create-net.json openshell sandbox create --name ov-net --policy /tmp/ov-policy.yaml \
  --detach --output json -- sleep 3600
for _ in $(seq 60); do
  openshell sandbox get ov-net --output json 2>/dev/null | grep -q '"Ready"' && break
  sleep 5
done
run net-sandbox-get.json openshell sandbox get ov-net --output json
run net-policy-base.json openshell policy get ov-net --base --output json
run net-policy-update.txt openshell policy update ov-net --rule-name ov_probe \
  --binary /usr/bin/curl --add-endpoint example.com:443:read-only:rest:enforce --wait
run net-policy-list.txt openshell policy list ov-net
run net-policy-base-v2.json openshell policy get ov-net --base --output json
run net-delete.txt openshell sandbox delete ov-net

run delete.txt openshell sandbox delete "$NAME"
run status-gateway-down.txt sh -c 'systemctl --user stop openshell-gateway && openshell status; openshell sandbox list -o json'
exit 0
