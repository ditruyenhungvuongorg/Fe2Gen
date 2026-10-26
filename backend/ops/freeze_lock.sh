#!/usr/bin/env bash
# Freeze the serving environment into backend/requirements.lock.txt (B21).
# Run on the Ubuntu host, from anywhere:  bash backend/ops/freeze_lock.sh
# Override the venv with VENV=/path/to/venv.
set -euo pipefail

VENV="${VENV:-$HOME/venvs/nckh-model1-span-v1}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="$HERE/../requirements.lock.txt"

if [ ! -f "$VENV/bin/activate" ]; then
  echo "venv not found: $VENV (set VENV=...)" >&2
  exit 1
fi
# shellcheck disable=SC1091
source "$VENV/bin/activate"

# --require-virtualenv makes pip refuse to freeze the system Python by mistake.
{
  echo "# Frozen $(date -u +%Y-%m-%dT%H:%M:%SZ) from $VENV on $(hostname)"
  echo "# python $(python --version 2>&1 | cut -d' ' -f2)"
  pip freeze --require-virtualenv --exclude-editable
} > "$OUT"
echo "wrote $OUT ($(grep -vc '^#' "$OUT") packages)"

cat <<MSG

Next steps
1. Review the file. Remove local-path or direct-URL lines ("pkg @ file:///...") that other hosts cannot install.
2. Commit it; CI (pip-audit) and Dependabot read it.
3. Optional, a lock with hashes for 'pip install --require-hashes':
     pip install pip-tools
     printf '%s\n' \$(grep -v '^#' "$OUT" | sed 's/==.*//') > /tmp/fe2gen-top.in
     pip-compile --generate-hashes --allow-unsafe --output-file="$HERE/../requirements.lock.hashes.txt" /tmp/fe2gen-top.in
     pip install --require-hashes -r backend/requirements.lock.hashes.txt
   For CUDA wheels (torch, xformers...) pip-compile needs the same --extra-index-url the host used;
   if hashes cannot be generated for them, keep the plain lock for those and hash-lock the rest.
MSG
