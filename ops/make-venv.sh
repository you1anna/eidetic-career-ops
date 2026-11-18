#!/usr/bin/env bash
# Makes .venv (Python 3.12 when available) if it is missing; idempotent and silent when it exists.
# Installs requirements-dev.txt (the runtime requirements plus the test runner). When that cannot
# install, as on the VPS Codex checkout without a package index, installs requirements.txt instead
# so checks still run under unittest. The SessionStart hook and ops/check.sh both call it. Exits
# non-zero when it cannot make a working environment.
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." || exit 1
[[ -x .venv/bin/python ]] && exit 0
if command -v python3.12 >/dev/null; then python3.12 -m venv .venv
elif command -v uv >/dev/null; then uv venv -q --seed --python 3.12 .venv 2>/dev/null || uv venv -q --seed .venv
else python3 -m venv .venv; fi || { echo "work-ops: could not create .venv" >&2; exit 1; }
if .venv/bin/python -m pip install -q -r requirements-dev.txt >/dev/null 2>&1; then
  echo "work-ops: created .venv ($(.venv/bin/python -V 2>&1)) with requirements-dev.txt"
elif .venv/bin/python -m pip install -q -r requirements.txt >/dev/null 2>&1; then
  echo "work-ops: created .venv ($(.venv/bin/python -V 2>&1)) with requirements.txt; pytest is not installed, so checks use unittest" >&2
else
  echo "work-ops: .venv created but requirements did not install; run .venv/bin/pip install -r requirements.txt" >&2
  exit 1
fi
