#!/usr/bin/env bash
# Feed each golden starter policy (tests/openshell/starter/*.yaml) to a real OpenShell: the
# sandbox must become Ready and its loaded policy must contain every generated rule (FR-11).
set -u
status=0
for file in tests/openshell/starter/*.yaml; do
  name="ov-starter-$(basename "$file" .yaml)"
  echo "=== $name ==="
  if ! openshell sandbox create --name "$name" --policy "$file" --detach --output json \
      -- sleep 600 > /dev/null; then
    echo "FAIL: OpenShell rejected $file"; status=1; continue
  fi
  ready=no
  for _ in $(seq 60); do
    if openshell sandbox get "$name" --output json 2>/dev/null | grep -q '"Ready"'; then
      ready=yes; break
    fi
    sleep 5
  done
  loaded=$(openshell policy get "$name" --base --output json)
  for rule in $(grep -E '^  [a-z0-9_]+:$' "$file" | tr -d ' :' | grep -v -E '^(read_only|read_write|include_workdir|compatibility)$'); do
    if ! grep -q "\"$rule\"" <<< "$loaded"; then
      echo "FAIL: rule $rule from $file is not in the loaded policy"; status=1
    fi
  done
  [ "$ready" = yes ] || { echo "FAIL: $name never became Ready"; status=1; }
  [ $status -eq 0 ] && echo "ok: $file accepted, $(grep -c '^  [a-z0-9_]*:$' "$file") keys"
  openshell sandbox delete "$name" > /dev/null
done
exit $status
