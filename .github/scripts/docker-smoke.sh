#!/usr/bin/env bash
set -euo pipefail
printf 'OJ_USERNAME=smoke\nOJ_PASSWORD=smoke\nAI_API_KEY=sk-smoke-test\nWEBUI_AUTH_TOKEN=ci-test-token\n' > .env
docker compose config --quiet
docker compose build
docker compose run --rm cli init --model smoke --base-url https://example.invalid/v1 --oj-root https://oj.invalid --profile economy
docker compose run --rm cli doctor --json
docker compose run --rm cli python -c "import os, shutil; from pathlib import Path; assert shutil.which('g++'); assert os.getuid() == 10001; assert Path('/data/config.json').exists()"
docker compose run --rm cli python -m pytest -q -p no:cacheprovider /app/test_agent.py::TestLocalJudgeCpp
docker compose up -d web
curl --fail --retry 15 --retry-delay 2 --retry-all-errors --retry-max-time 60 http://127.0.0.1:5000/status
curl --fail -H 'X-Auth-Token: ci-test-token' -H 'Content-Type: application/json' -d '{"contest_start_page":3}' http://127.0.0.1:5000/config
docker compose run --rm cli python -c "from config_manager import ConfigManager; assert ConfigManager().contest_start_page == 3"
docker compose up -d --force-recreate web
curl --fail --retry 15 --retry-delay 2 --retry-all-errors --retry-max-time 60 -H 'X-Auth-Token: ci-test-token' http://127.0.0.1:5000/config/doctor
docker compose run --rm cli python -c "from config_manager import ConfigManager; assert ConfigManager().contest_start_page == 3"
