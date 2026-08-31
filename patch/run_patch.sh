#!/usr/bin/env bash
# Start the in-memory readout speedup using the local .venv.
#
# Usage:  patch/run_patch.sh <scope-ip> [extra patch_scope.py args...]
# Example: patch/run_patch.sh 172.30.188.217
#
# Leave this running while you capture; Ctrl-C reverts the scope to stock.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "$here/.." && pwd)"
py="$root/.venv/bin/python"

if [[ ! -x "$py" ]]; then
    echo "error: $py not found." >&2
    echo "create it from the repo root with:" >&2
    echo "  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
    exit 1
fi

if [[ $# -lt 1 ]]; then
    echo "usage: $0 <scope-ip> [extra args]" >&2
    exit 2
fi

exec "$py" "$here/patch_scope.py" "$@"
