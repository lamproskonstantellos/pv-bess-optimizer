#!/usr/bin/env bash
# Run every audit driver under scripts/audit_runs/ in sequence, time
# each one, and exit non-zero on the first failure.
set -euo pipefail

cd "$(dirname "$0")/../.."

mkdir -p scripts/audit_runs/results

for driver in scripts/audit_runs/run_*.py; do
    echo "=== $(basename "$driver") ==="
    time python "$driver"
done

echo "All 10 drivers complete."
