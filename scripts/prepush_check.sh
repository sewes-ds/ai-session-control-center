#!/usr/bin/env bash
set -euo pipefail

ROOT_INPUT="${1:-$(pwd)}"
ROOT="$(cd "$ROOT_INPUT" && pwd -P)"

cd "$ROOT"

echo "Checking forbidden runtime files..."
if find "$ROOT" \
  \( -path '*/.git/*' -o -path '*/.git' -o -path '*/.venv/*' -o -path '*/__pycache__/*' \) -prune -o \
  \( \
    -name '.env' -o \
    -name 'auth.json' -o \
    -iname '*secret*' -o \
    -iname '*token*' -o \
    -iname '*key*' \
  \) -print | grep -q .; then
  echo "ERROR: forbidden files detected"
  find "$ROOT" \
    \( -path '*/.git/*' -o -path '*/.git' -o -path '*/.venv/*' -o -path '*/__pycache__/*' \) -prune -o \
    \( \
      -name '.env' -o \
      -name 'auth.json' -o \
      -iname '*secret*' -o \
      -iname '*token*' -o \
      -iname '*key*' \
    \) -print
  exit 1
fi

SECRET_HITS_FILE="$(mktemp)"
trap 'rm -f "$SECRET_HITS_FILE"' EXIT

if grep -RIlE \
  '(xox[baprs]-[A-Za-z0-9-]{10,}|gh[pousr]_[A-Za-z0-9_]{30,}|sk-[A-Za-z0-9]{20,}|[0-9]{8,12}:[A-Za-z0-9_-]{30,}|Bearer[[:space:]]+[A-Za-z0-9._-]{20,}|refresh_token[[:space:]]*[:=][[:space:]]*["'\''"]?[A-Za-z0-9._-]{20,}|access_token[[:space:]]*[:=][[:space:]]*["'\''"]?[A-Za-z0-9._-]{20,})' \
  "$ROOT" \
  --exclude-dir='.git' \
  --exclude-dir='.venv' \
  --exclude-dir='__pycache__' \
  --exclude='*.md' \
  --exclude='.env.example' \
  --exclude='test_*.py' >"$SECRET_HITS_FILE"; then
  echo "ERROR: possible hardcoded secret detected"
  sed -n '1,80p' "$SECRET_HITS_FILE"
  exit 1
fi

echo "OK"
