#!/usr/bin/env bash
set -euo pipefail
# Never destroy a developer's real .env if this script is run locally.
ENV_BACKUP="$(mktemp)"
HAD_ENV=0
if [ -f .env ]; then cp .env "$ENV_BACKUP"; HAD_ENV=1; fi
cleanup() {
  if [ "$HAD_ENV" = "1" ]; then
    cp "$ENV_BACKUP" .env
  elif [ -z "${CI:-}" ]; then
    rm -f .env            # local run without .env: remove the smoke file
  fi                      # CI keeps it for the workflow's logs/cleanup step
  rm -f "$ENV_BACKUP"
}
trap cleanup EXIT
printf 'OJ_USERNAME=smoke\nOJ_PASSWORD=smoke\nAI_API_KEY=sk-smoke-test\nWEBUI_AUTH_TOKEN=ci-test-token\n' > .env
docker compose config --quiet
docker compose build
docker compose run --rm cli init --model smoke --base-url https://example.invalid/v1 --oj-root https://oj.invalid --profile economy
docker compose run --rm cli doctor --json
docker compose run --rm cli python -c "import os, shutil; from pathlib import Path; import hydroai; from hydroai.web.app import TEMPLATE_PATH; from hydroai.ai.client import AIClient; assert shutil.which('g++'); assert os.getuid() == 10001; assert Path('/data/config.json').exists(); assert TEMPLATE_PATH.exists(); assert AIClient._load_prompts()"
docker compose run --rm cli python -c "from hydroai.usage.store import get_store; s=get_store(); assert s.record(model='smoke', usage={'input': 10, 'output': 5}); assert s.summary()['requests'] >= 1"
docker compose run --rm cli usage --json
docker compose run --rm cli quota --json
docker compose up -d web
curl --fail --retry 15 --retry-delay 2 --retry-all-errors --retry-max-time 60 http://127.0.0.1:5000/status
curl --fail -H 'X-Auth-Token: ci-test-token' -H 'Content-Type: application/json' -d '{"contest_start_page":3}' http://127.0.0.1:5000/config
curl --fail -H 'X-Auth-Token: ci-test-token' 'http://127.0.0.1:5000/api/usage/summary?days=7'
curl --fail -H 'X-Auth-Token: ci-test-token' http://127.0.0.1:5000/api/quota
docker compose run --rm cli python -c "from hydroai.config.manager import ConfigManager; assert ConfigManager().contest_start_page == 3"
docker compose up -d --force-recreate web
curl --fail --retry 15 --retry-delay 2 --retry-all-errors --retry-max-time 60 -H 'X-Auth-Token: ci-test-token' http://127.0.0.1:5000/config/doctor
docker compose run --rm cli python -c "from hydroai.config.manager import ConfigManager; assert ConfigManager().contest_start_page == 3"
