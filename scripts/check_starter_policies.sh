#!/usr/bin/env bash
# Feed each golden starter policy (tests/openshell/starter/*.yaml) to a real OpenShell: the
# sandbox must become Ready and its loaded policy must contain every generated rule (FR-11).
set -u
status=0
for file in tests/openshell/starter/*.yaml; do
  name="ovs-$(basename "$file" .yaml | cut -c1-12)"  # OpenShell 0.1.4: names are <= 19 characters
  echo "=== $name ($file) ==="
  if ! openshell sandbox create --name "$name" --policy "$file" --detach --output json \
      -- sleep 600 > /dev/null; then
    echo "FAIL: OpenShell rejected $file"; status=1; continue
  fi
  failed=0
  ready=no
  for _ in $(seq 60); do
    if openshell sandbox get "$name" --output json 2>/dev/null | grep -q '"Ready"'; then
      ready=yes; break
    fi
    sleep 5
  done
  [ "$ready" = yes ] || { echo "FAIL: $name never became Ready"; failed=1; }
  loaded=$(openshell policy get "$name" --base --output json)
  rules=$(grep -E '^  [a-z0-9_]+:$' "$file" | tr -d ' :' | grep -v -E '^(read_only|read_write)$')
  for rule in $rules; do
    grep -q "\"$rule\"" <<< "$loaded" || { echo "FAIL: rule $rule is not loaded"; failed=1; }
  done
  [ $failed -eq 0 ] && echo "ok: accepted, Ready, rules loaded: $(echo $rules | tr '\n' ' ')"
  status=$((status | failed))
  openshell sandbox delete "$name" > /dev/null
done
exit $status
