#!/usr/bin/env bash
set -euo pipefail
ENV_FILE=config/.env
mkdir -p config

# Never destroy a developer's real config/.env if this script is run locally.
ENV_BACKUP="$(mktemp)"
HAD_ENV=0
if [ -f "$ENV_FILE" ]; then cp "$ENV_FILE" "$ENV_BACKUP"; HAD_ENV=1; fi
cleanup() {
  if [ "$HAD_ENV" = "1" ]; then
    cp "$ENV_BACKUP" "$ENV_FILE"
  elif [ -z "${CI:-}" ]; then
    rm -f "$ENV_FILE"        # local run without .env: remove the smoke file
  fi                          # CI keeps it for the workflow's logs/cleanup step
  rm -f "$ENV_BACKUP"
}
trap cleanup EXIT

printf 'OJ_USERNAME=smoke\nOJ_PASSWORD=smoke\nAI_API_KEY=sk-smoke-test\nWEBUI_AUTH_TOKEN=ci-test-token\n' > "$ENV_FILE"
compose() { docker compose --env-file "$ENV_FILE" "$@"; }

compose config --quiet
compose build
compose run --rm cli init --model smoke --base-url https://example.invalid/v1 --oj-root https://oj.invalid --profile economy
compose run --rm cli doctor --json
compose run --rm cli python -c "import os, shutil; from pathlib import Path; import hydroai; from hydroai.web.app import TEMPLATE_PATH; from hydroai.ai.client import AIClient; assert shutil.which('g++'); assert os.getuid() == 10001; assert Path('/data/config.json').exists(); assert TEMPLATE_PATH.exists(); assert AIClient._load_prompts()"
compose run --rm cli python -c "from hydroai.usage.store import get_store; s=get_store(); assert s.record(model='smoke', usage={'input': 10, 'output': 5}); assert s.summary()['requests'] >= 1"
compose run --rm cli usage --json
compose run --rm cli quota --json
compose up -d web
curl --fail --retry 15 --retry-delay 2 --retry-all-errors --retry-max-time 60 http://127.0.0.1:5000/status
curl --fail -H 'X-Auth-Token: ci-test-token' -H 'Content-Type: application/json' -d '{"contest_start_page":3}' http://127.0.0.1:5000/config
curl --fail -H 'X-Auth-Token: ci-test-token' 'http://127.0.0.1:5000/api/usage/summary?days=7'
curl --fail -H 'X-Auth-Token: ci-test-token' http://127.0.0.1:5000/api/quota
compose run --rm cli python -c "from hydroai.config.manager import ConfigManager; assert ConfigManager().contest_start_page == 3"
compose up -d --force-recreate web
curl --fail --retry 15 --retry-delay 2 --retry-all-errors --retry-max-time 60 -H 'X-Auth-Token: ci-test-token' http://127.0.0.1:5000/config/doctor
compose run --rm cli python -c "from hydroai.config.manager import ConfigManager; assert ConfigManager().contest_start_page == 3"
