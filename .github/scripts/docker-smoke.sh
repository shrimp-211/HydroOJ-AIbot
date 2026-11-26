#!/usr/bin/env bash
set -euo pipefail
ENV_FILE=config/.env
mkdir -p config

# Never destroy a developer's real config/.env if this script is run locally.
ENV_BACKUP="$(mktemp)"
COOKIE_JAR="$(mktemp)"
HAD_ENV=0
if [ -f "$ENV_FILE" ]; then cp "$ENV_FILE" "$ENV_BACKUP"; HAD_ENV=1; fi
cleanup() {
  if [ "$HAD_ENV" = "1" ]; then
    cp "$ENV_BACKUP" "$ENV_FILE"
  elif [ -z "${CI:-}" ]; then
    rm -f "$ENV_FILE"        # local run without .env: remove the smoke file
  fi                          # CI keeps it for the workflow's logs/cleanup step
  rm -f "$ENV_BACKUP" "$COOKIE_JAR"
}
trap cleanup EXIT

printf 'OJ_USERNAME=smoke\nOJ_PASSWORD=smoke\nAI_API_KEY=sk-smoke-test\nWEBUI_USERNAME=smoke\nWEBUI_PASSWORD=smoke\nWEBUI_AUTOSTART_DAEMON=0\n' > "$ENV_FILE"
login_web() {
  curl --fail -sS -c "$COOKIE_JAR" -d 'username=smoke&password=smoke' \
    http://127.0.0.1:5000/login -o /dev/null
}
compose() { docker compose --env-file "$ENV_FILE" "$@"; }

compose config --quiet
compose build
compose run --rm cli init --model smoke --base-url https://example.invalid/v1 --oj-root https://oj.invalid --profile economy
compose run --rm cli doctor --json
compose run --rm cli python -c "import os, shutil; from pathlib import Path; from hydroai.web.app import app; from hydroai.ai.client import AIClient; assert shutil.which('g++'); assert os.getuid() == 10001; assert Path('/data/config.json').exists(); assert all(app.jinja_env.get_template(name) for name in ('index.html', 'login.html')); assert AIClient._load_prompts()"
compose run --rm cli python -c "from hydroai.usage.store import get_store; s=get_store(); assert s.record(model='smoke', usage={'input': 10, 'output': 5}); assert s.summary()['requests'] >= 1"
compose run --rm cli usage --json
compose run --rm cli quota --json
compose up -d web
curl --fail --retry 15 --retry-delay 2 --retry-all-errors --retry-max-time 60 http://127.0.0.1:5000/status
login_web
curl --fail -b "$COOKIE_JAR" -H 'Content-Type: application/json' -d '{"contest_start_page":3}' http://127.0.0.1:5000/config
curl --fail -b "$COOKIE_JAR" 'http://127.0.0.1:5000/api/usage/summary?days=7'
curl --fail -b "$COOKIE_JAR" http://127.0.0.1:5000/api/quota
compose run --rm cli python -c "from hydroai.config.manager import ConfigManager; assert ConfigManager().contest_start_page == 3"
compose up -d --force-recreate web
curl --fail --retry 15 --retry-delay 2 --retry-all-errors --retry-max-time 60 http://127.0.0.1:5000/status
login_web
curl --fail -b "$COOKIE_JAR" http://127.0.0.1:5000/config/doctor
compose run --rm cli python -c "from hydroai.config.manager import ConfigManager; assert ConfigManager().contest_start_page == 3"
