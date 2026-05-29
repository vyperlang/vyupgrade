#!/usr/bin/env bash
set -euo pipefail

rm -rf dist
uv build

tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT
export PIP_CACHE_DIR="$tmpdir/pip-cache"

python3 -m venv "$tmpdir/venv"
"$tmpdir/venv/bin/python" -m pip install --upgrade pip >/dev/null

wheel=(dist/vyupgrade-*.whl)
"$tmpdir/venv/bin/python" -m pip install "${wheel[0]}" >/dev/null

contract="$tmpdir/Noop.vy"
cat > "$contract" <<'EOF'
#pragma version 0.4.3

@external
def f(x: uint256) -> uint256:
    return x
EOF

"$tmpdir/venv/bin/vyupgrade" --help >/dev/null
"$tmpdir/venv/bin/vyupgrade" "$contract" --source-version 0.4.3 --check
