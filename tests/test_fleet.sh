#!/usr/bin/env bash
set -eu

repo=$(cd "$(dirname "$0")/.." && pwd)
export PYTHONDONTWRITEBYTECODE=1

"$repo/bin/fleet" check
for test in "$repo"/tests/test_*.py; do
  python3 "$test" "$repo"
done
