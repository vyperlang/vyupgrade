#!/usr/bin/env sh
set -eu

run_smoke() {
  set +e
  uv run python -m vyupgrade.cli "$1" --report-json "$2"
  status="$?"
  set -e
  if [ "$status" != "0" ] && [ "$status" != "3" ]; then
    exit "$status"
  fi
}

run_smoke /Users/banteg/dev/yearn/yearn-vaults-v2/contracts/Vault.vy /private/tmp/vyupgrade-yearn-v2.json
run_smoke /Users/banteg/dev/yearn/yearn-vaults-v3/contracts/VaultV3.vy /private/tmp/vyupgrade-yearn-v3.json
