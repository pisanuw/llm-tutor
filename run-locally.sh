#!/usr/bin/env bash
#
# run-locally.sh: launch the tutor web app on your machine for manual testing.
#
# SAFETY: if your .env points DATABASE_URL at a production database, this
# script still forces a throwaway local SQLite DB instead, so nothing you click
# here can touch real student data. (python-dotenv does not override an env var
# that is already set, so exporting DATABASE_URL="" below wins over .env.)
#
# Usage:
#   ./run-locally.sh              # http://localhost:8000  (port 8000)
#   ./run-locally.sh 8080         # pick a different port
#
# Then open:   http://localhost:8000/auth/login
# Slides only: http://localhost:8000/slides/AST101/lecture-01/index.html (after render-html)
#
set -euo pipefail

cd "$(dirname "$0")"

PORT="${1:-8000}"

# --- prerequisites -----------------------------------------------------------
if [[ ! -x .venv/bin/uvicorn ]]; then
  echo "ERROR: .venv/bin/uvicorn not found. Build the web venv first (python3.11" >&2
  echo "       + fastapi/uvicorn/jinja2/sqlalchemy/anthropic/chromadb...)."      >&2
  exit 1
fi
if [[ ! -f .env ]]; then
  echo "ERROR: .env missing (needs at least ANTHROPIC_API_KEY, SECRET_KEY, ADMIN_TOKEN)." >&2
  exit 1
fi

# --- force a safe, local-only configuration ----------------------------------
# Empty DATABASE_URL -> get_engine() falls back to SQLite at TUTOR_DB.
export DATABASE_URL=""
export TUTOR_DB="tutor-local.db"
export BASE_URL="http://localhost:${PORT}"

echo "──────────────────────────────────────────────────────────────"
echo " Tutor local dev server"
echo "   DB        : sqlite:///${TUTOR_DB}  (no production database touched)"
echo "   Base URL  : ${BASE_URL}"
echo "   Login     : ${BASE_URL}/auth/login"
echo "   Admin     : ${BASE_URL}/admin/login  (paste ADMIN_TOKEN)"
echo "   Slides    : ${BASE_URL}/slides/AST101/lecture-01/index.html"
echo "──────────────────────────────────────────────────────────────"
echo " Login: Google OAuth or a magic link (without RESEND_API_KEY the link is"
echo "        shown on the page). Addresses in ADMIN_EMAILS bypass the whitelist."
echo " Press Ctrl-C to stop."
echo

exec .venv/bin/uvicorn tutor.main:app --reload --port "${PORT}"
