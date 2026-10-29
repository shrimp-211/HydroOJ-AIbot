# HydroOJ AIbot

为 Hydro OJ 提供 AI 解题、比赛监控、标程题解生成与测试数据补充。支持多模型路由、本地编译与对拍、结构化题解、token / 费用统计，以及 Web 配置和统计页面。

## 功能与运行方式

| 功能 | 入口 | 说明 |
| --- | --- | --- |
| 单题求解 | `oj_solver.py` | 获取题面、生成代码、验证、提交，AC 后发布题解 |
| 比赛 / 训练批量处理 | `contest_solver.py` | 多线程求解指定链接中的题目 |
| 比赛监控 | `contest_daemon.py` | 轮询多域比赛，支持控制台和 OJ 私信指令 |
| 标程题解 | `benchmark_solver.py` | 扫描指定用户的满分提交，解读代码并发布 |
| 测试数据补充 | `testdata_supplement.py` | 为缺数据题目生成样例、标程和生成器 |
| 配置初始化 / 诊断 | `config_cli.py` | 生成单模型配置，离线检查参数与密钥引用 |
| Web 面板 | `webui.py` | 配置、诊断、统计与运行日志 |
| OneBot 接入 | `onebot_server.py` | 接收消息指令并调用求解脚本 |

所有命令请在项目目录运行。Docker 已设置独立的数据目录，无需手动管理工作路径。

## 本地安装

需要 Python **3.10+**。C++ 本地验证需要 `g++` 或 `clang++`；没有工具链时会回退到原求解流程。Python 本地验证使用当前 Python 环境。

```bash
git clone https://github.com/shrimp-211/HydroOJ-AIbot.git
cd HydroOJ-AIbot
python -m venv .venv
```

激活虚拟环境：

```bash
# Linux / macOS
source .venv/bin/activate
# Windows PowerShell
.\.venv\Scripts\Activate.ps1
```

安装依赖并初始化。把下列模型名和两个 URL 换成实际值：

```bash
pip install -r requirements.txt
python config_cli.py init --model YOUR_MODEL --base-url https://your-api.example/v1 --oj-root https://your-oj.example --profile economy
```

`init` 创建 `config.json` 和空白 `.env`，不会覆盖已有文件。在 `.env` 填写：

```dotenv
OJ_USERNAME=your_username
OJ_PASSWORD=your_password
AI_API_KEY=your_api_key
```

然后检查并尝试获取一道题：

```bash
python config_cli.py doctor
python oj_solver.py https://your-oj.example/p/1000 --dry-run
python oj_solver.py https://your-oj.example/p/1000
```

`doctor` 不访问 OJ 或 AI 服务，也不产生模型费用；通过只表示本地配置完整，不代表账号权限或远端服务可用。`--dry-run` 只获取题面，不请求 AI。

已有配置可直接保留，也可参考 [完整配置示例](config.example.json)。指定其他配置文件执行初始化或诊断：

```bash
python config_cli.py --config path/to/config.json doctor --json
```

## Docker / Compose 部署

需要能运行 Linux 容器的 Docker Engine / Docker Desktop，以及 Docker Compose v2。仓库提供 `Dockerfile`、`compose.yaml`、`.dockerignore` 和 [.env.docker.example](.env.docker.example)。镜像基于 Python 3.12，包含 `g++`，以 UID/GID **10001:10001** 运行。

### 首次启动

在仓库目录复制环境变量模板：

```bash
# Linux / macOS
cp .env.docker.example .env
# Windows PowerShell
Copy-Item .env.docker.example .env
```

填写 `.env` 的 `OJ_USERNAME`、`OJ_PASSWORD`、`AI_API_KEY`、`WEBUI_AUTH_TOKEN`。
`WEBUI_AUTH_TOKEN` 请设置为自行生成的随机字符串；Compose 会拒绝使用空令牌启动。
已有 `.env` 时保留它并补充 `WEBUI_AUTH_TOKEN`，不要用模板覆盖凭据。

```bash
docker compose build
docker compose run --rm cli init --model YOUR_MODEL --base-url https://your-api.example/v1 --oj-root https://your-oj.example --profile economy
docker compose run --rm cli doctor
docker compose up -d web
```

打开 [http://127.0.0.1:5000](http://127.0.0.1:5000)，进入「配置 → 访问设置」，填写 `WEBUI_AUTH_TOKEN` 后刷新配置。
`.env` 的 `WEBUI_PORT` 可修改宿主机端口，例如 `WEBUI_PORT=8080`。

默认只启动 Web 面板。要开启比赛自动处理，再执行：

```bash
docker compose --profile worker up -d
```

守护进程默认包含进行中的比赛。只处理已结束的比赛，可把 `compose.yaml` 的 daemon 命令改为：

```yaml
command: [daemon, --no-interactive, --ended-only]
```

Compose 的 profiles 用法见 [Docker 官方文档](https://docs.docker.com/compose/how-tos/profiles/)。

### 容器命令

`cli` 仅用于一次性任务，不会随普通 `up` 自动启动：

```bash
docker compose run --rm cli doctor --json
docker compose run --rm cli solve https://your-oj.example/p/1000
docker compose run --rm cli contest https://your-oj.example/contest/CONTEST_ID --workers 2
docker compose run --rm cli benchmark --start-page 2
docker compose run --rm cli testdata https://your-oj.example/p/1000
docker compose run --rm cli daemon --once --no-msg-backend --start-page 1
```

容器入口还支持 `web`、`onebot`、`config` 和 `python`。例如：

```bash
docker compose run --rm cli config --help
docker compose run --rm cli solve --help
```

直接使用 Docker 时，使用同一命名卷并传入 `.env`：

```bash
docker build -t hydrooj-aibot:local .
docker run --rm --init --env-file .env -v hydrooj-data:/data hydrooj-aibot:local init --model YOUR_MODEL --base-url https://your-api.example/v1 --oj-root https://your-oj.example
docker run --rm --init --env-file .env -v hydrooj-data:/data hydrooj-aibot:local doctor
```

直接 Docker 示例与 Compose 创建的卷相互独立；请固定使用同一种部署方式或明确迁移数据。

### 数据、升级与备份

- `/app`：镜像中的代码与提示词；升级镜像会更新它们。
- `/tmp`：可执行的临时内存文件系统，供本地编译、样例验证和数据生成使用，容器停止后清空。
- `/data`：命名卷，保存 `config.json`、费用统计、处理记录、Cookie、日志、本地验证目录和生成数据。
- 宿主机 `.env`：由 Compose 注入环境变量，不会复制到镜像。变量优先于卷内 JSON 配置；修改后需要重新创建服务。

```bash
# 查看状态和日志
docker compose ps
docker compose logs -f web daemon

# 拉取更新、重建并启动
git pull --ff-only
docker compose build
docker compose --profile worker up -d --force-recreate

# 停止服务，保留数据卷
docker compose --profile worker down
```

备份时先停止 daemon，保持 web 容器可用于复制，避免后台任务同时写入。创建宿主机 `backups` 目录后执行：

```bash
docker compose stop daemon
docker compose cp web:/data/. ./backups/
```

恢复到现有 web 容器时：

```bash
docker compose stop daemon
docker compose cp ./backups/. web:/data/
docker compose run --rm --user 0 --cap-add CHOWN --entrypoint chown cli -R 10001:10001 /data
docker compose --profile worker up -d
```

`down -v` 会删除数据卷；普通 `down` 会保留数据。详细卷管理见 [Docker 数据卷文档](https://docs.docker.com/engine/storage/volumes/)。
默认端口仅绑定宿主机 `127.0.0.1`；远程访问建议使用 SSH 隧道或带 HTTPS 的反向代理。Web 服务用于管理面板，当前使用 Werkzeug 服务端。

## 比赛列表：默认从第 2 页获取

守护进程和标程全量扫描共用同一分页实现，首次请求为：

```text
/d/<domain>/contest?page=2&limit=50
```

起始页包含在扫描范围内，后续继续逐页读取，最多读取 20 页；遇到空页、短页或重复页停止。
**默认跳过第 1 页**；需要包含最新首页比赛时，设置为 `1`。指定某个比赛完整链接求解不受此设置影响。

三种配置方式，优先级为 **命令行 > 环境变量 > config.json > 默认 2**：

```json
{
  "contest_start_page": 2
}
```

```dotenv
OJ_CONTEST_START_PAGE=1
```

```bash
python contest_daemon.py --start-page 1
python benchmark_solver.py --start-page 3
# Docker 单次运行覆盖
docker compose run --rm cli daemon --once --start-page 1
```

Web 配置页的「监控守护 → 比赛列表起始页」可直接修改 JSON 值。必须填写大于等于 1 的整数。

## AI 配置与 token 预算

### 初始化预设

| 预设 | 难度判断 | 每层候选思路 | 本地验证、对拍、结构化题解 |
| --- | --- | ---: | --- |
| `economy` | 关闭 | 2 | 保留 |
| `balanced` | 开启 | 3 | 保留 |

预设只在 `init` 时写入设置，之后可以逐项调整。单模型无需额外的多模型路由设置。
`--provider` 可指定 `compatible`（默认）、`openai`、`deepseek`、`zhipu`。

### 配置优先级与凭据

命令行 / 运行时覆盖 > 环境变量 > JSON > 默认值。命令行覆盖在热重载后仍有效；JSON 解析失败时保留上一份有效配置。
模型独立字段覆盖全局 AI 默认值。默认读当前工作目录的配置；`OJ_DATA_DIR` 可指定独立数据目录，Docker 固定为 `/data`。

推荐在 JSON 中引用环境变量，不直接保存密钥：

```json
{
  "ai_api_key": "env:AI_API_KEY",
  "models": {
    "YOUR_MODEL": {
      "base_url": "https://your-api.example/v1",
      "api_key": "env:PROVIDER_API_KEY",
      "provider": "compatible",
      "max_tokens": 16384
    }
  }
}
```

支持 `env:NAME`、`${NAME}` 和旧的大写变量名格式。显式引用缺失时不会使用其他服务的全局密钥。
Web 配置接口不返回密码或模型密钥原文；留空表示保留现有凭据。

### 获取模型列表与新增连接

参考 cc-switch 的模型发现流程，bot 可以查询已保存连接的模型列表，不请求内容生成：

```bash
python config_cli.py models --json
python config_cli.py models --model YOUR_CONFIG_NAME
# Docker
docker compose run --rm cli config models --json
```

API 地址支持粘贴完整的 `/chat/completions` 或 `/models` 地址，程序会还原基础地址，保留自定义路径前缀。
仅在根地址的 `/models` 返回 404/405 时尝试 `/v1/models`。返回的 `base_url` 是查询成功的基础地址。
鉴权失败不会继续探测其他端点；请求不跟随重定向，密钥不会被转发给另一个站点。
模型配置可指定同源的 `models_url`；列表支持 `data` / `models` 数组。

新增连接只保存密钥环境变量引用，例如：

```bash
python config_cli.py add-model fast --model-id YOUR_API_MODEL_ID --base-url https://your-api.example/v1 --api-key-env MY_PROVIDER_KEY --provider compatible
```

先在 `.env` 设置 `MY_PROVIDER_KEY`；Docker 需要将新增变量注入容器后重新创建服务。
`fast` 是本地配置别名，`model_id` 是真正发给 API 的模型 ID。重复配置名会被拒绝，原连接不会被覆盖。
加入 `--set-default` 会将它设为默认，并切换为该模型的单模型路由；需要多模型时继续配置 `model_router.tiers`。
Web 的「配置 → 接入模型」提供相同流程：选择已有连接获取列表，再填写别名、模型 ID、地址及密钥变量名。

模型列表不保证其中每个模型都支持 Chat Completions。当前仍只支持该兼容协议；Anthropic Messages / Gemini 原生协议、OAuth 订阅接入未包含在此功能中。

### 自动获取价格

价格目录使用 [models.dev](https://models.dev)，参考 [cc-switch 定价与接入研究记录](docs/cc-switch-reference.md) 独立实现。
源价格单位是 **USD / 百万 token**，项目既有统计与预算单位是 **CNY**，因此必须显式维护换算率：

```json
{
  "pricing_auto_update": true,
  "pricing_refresh_hours": 6,
  "pricing_usd_to_cny": 0.0
}
```

`0.0` 表示未设置汇率；请替换成你采用的 **1 USD 对应 CNY 数额**，这不是实时汇率查询。
也可设置 `PRICING_USD_TO_CNY` 环境变量。美元金额只乘一次汇率，token 计费再除以一百万。

```bash
# 从公共目录更新价格缓存，不发送 OJ 凭据或 AI Key
python config_cli.py prices --refresh
# 查看本地价格、来源、是否过期以及未匹配原因；不联网
python config_cli.py prices --json
# Docker
docker compose run --rm cli config prices --refresh
```

CLI 的 `prices --usd-to-cny RATE` 可保存汇率；Web 的「自动定价」可以设置汇率、刷新间隔并查看结果。

- 手工 `models.<name>.pricing` 优先，包括显式免费价格；自动同步不改写这部分配置。
- 正常运行时按需刷新，默认间隔 6 小时；缓存写入配置文件同目录的 `model_prices.json`，Docker 中位于持久化 `/data`。
- 网络失败时保留最后有效缓存，并至少等待 60 秒再尝试。无法匹配或缺少汇率时标记价格未知，不把它解释成免费。
- `doctor` 和价格查看保持离线；`prices --refresh` / Web 刷新按钮才立即访问公共目录。
- 按供应商和模型 ID 精确匹配。官方主机或目录中唯一匹配的 API 主机可识别供应商；无法识别的代理服务必须显式指定 `pricing_provider`，如 `openai`、`deepseek`、`zhipuai`。`provider` 仅表示协议，不能证明代理的实际收费标准。
- 通过 `pricing_model` 映射代理别名，不会按模糊名称或不明后缀套用另一模型价格。未指定时使用 `model_id`，再回退到配置名称。
- 公共分档价格暂不自动套用，需手工填写适用费率。公共报价、手工汇率与网关折扣可能与实际账单不同，未知价格或供应商缺失 usage 时，费用上限不能保证生效。

示例：代理模型使用 OpenAI 目录中的某个价格作为估算：

```json
{
  "models": {
    "fast": {
      "model_id": "YOUR_PROXY_MODEL_ID",
      "base_url": "https://your-api.example/v1",
      "api_key": "env:MY_PROVIDER_KEY",
      "provider": "compatible",
      "pricing_provider": "openai",
      "pricing_model": "EXACT_MODELS_DEV_MODEL_ID"
    }
  }
}
```

### 模型协议参数

这些可选字段位于 `models.<模型名>`：

| 字段 | 用途 |
| --- | --- |
| `model_id` | 实际 API 模型 ID；配置名称可以使用本地别名 |
| `models_url` | 可选的同源模型列表地址 |
| `pricing_provider` / `pricing_model` | 定价目录供应商与精确模型 ID |
| `provider` | `auto` / `openai` / `deepseek` / `zhipu` / `compatible`；auto 只识别官方主机名 |
| `token_parameter` | `max_tokens` 或 `max_completion_tokens`；默认按 provider 选择 |
| `reasoning_effort` | 模型支持的推理强度；留空不传 |
| `supports_reasoning_effort` | 是否传推理强度；OpenAI / 代理模型需按能力显式开启 |
| `thinking` | `enabled` / `disabled`，控制 DeepSeek / 智谱风格 thinking 参数 |
| `stream_usage` | 默认 true；不接受 `stream_options` 的网关可关闭，关闭后可能没有流式用量 |
| `extra_body` | 供应商扩展字段；不能覆盖模型、消息、流式开关或输出上限 |
| `timeout` / `max_retries` | SDK 超时与重试，默认 180 秒 / 2 次 |
| `vision` | 是否支持题面图片输入 |
| `pricing` | 每百万 token 的 input / output / cache_hit 价格，支持峰谷价 |

DeepSeek / 智谱官方端点保留旧配置的 thinking 默认行为。代理端点应显式声明能力。
参数和流式用量处理参考 [Chat Completions 接口文档](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)。本项目使用 Chat Completions 兼容协议，非兼容服务需要额外适配。

### 分任务输出预算

```json
{
  "ai_task_tokens": {
    "default": 16384,
    "solve": 16384,
    "plan": 4096,
    "reference": 8192,
    "difficulty": 1024,
    "editorial": 8192
  },
  "compact_history": true
}
```

实际请求取任务预算与模型 `max_tokens` 的较小值。预算包含接口计入输出的推理 token；复杂题可提高 `solve` 和 `plan`。
被截断的输出不会作为完整代码提交。降低上限不保证按比例减少实际费用，实际消耗取决于题目、模型和重试次数。

其他节省方式：首轮 AC 后跳过难度请求；修正仅保留完整题面、当前代码、最近失败摘要和最新反馈；题解模型只写讲解，AC 代码由程序插入。
`compact_history: false` 可恢复完整修复历史。流式计量支持最后一个无 choices 的 usage 包；缓存命中 token 不再重复按普通输入价计费。

### 多模型与难度路由

`model_router.tiers` 定义 `flash` / `pro` / `max` 对应模型，名字须与模型表一致。
难度 1–2 从 flash 开始，3–5 从 pro 开始，6–8 从 max 开始；失败后逐级尝试。
配置 `difficulty_detect_enable: false` 可省去难度请求；`difficulty_skip_model` 可指定直接使用的模型。
若它不在路由表中，本次只使用该模型。

`ai_vision_model` 指定题面含图时的模型。实际可用模型、输出限制和价格由服务商决定，示例中的名称和价格不是实时查询结果。

## 求解、验证与题解

默认 Agent 流程：方案分析 → 完整代码 → 本地编译 → 样例验证 → 随机对拍 → OJ 评测。
失败后使用编译日志、样例差异、反例或评测信息修正；仍未通过则换思路或模型。

| 配置 | 默认 | 含义 |
| --- | ---: | --- |
| `agent_enabled` | true | 本地验证流程开关 |
| `solve_candidates` | 3 | 每个模型层级的候选思路数量 |
| `solve_repairs` | 2 | 每条思路最多修正次数 |
| `agent_max_steps` | 12 | 单次尝试的实现 / 修正轮数上限 |
| `agent_max_submissions` | 8 | 单次尝试提交上限 |
| `agent_max_seconds` | 900 | 单次尝试时间上限 |
| `agent_stress_enable` / `agent_stress_rounds` | true / 30 | 随机对拍开关与轮数 |
| `editorial_enable` / `editorial_fix_rounds` | true / 2 | AC 后写题解及修复轮数 |
| `editorial_model` | 空 | 留空沿用求解模型，也可单独指定 |

题解包含题意、思路、正确性、复杂度、实现要点与代码；程序保证代码块与评测通过的代码一致。
本地验证会执行 AI 生成的程序。超时、资源限制和 Docker 非 root 运行不能替代专用的代码沙箱，应在专用环境运行。

## 常用命令

```bash
# 单题：默认提交并在 AC 后发布题解
python oj_solver.py 1000
python oj_solver.py https://your-oj.example/d/custom/p/1000 --stream
python oj_solver.py 1000 --no-post
python oj_solver.py 1000 --no-submit
python oj_solver.py 1000 --no-agent
python oj_solver.py 1000 --no-stress
python oj_solver.py 1000 --no-difficulty-detect
python oj_solver.py 1000 --difficulty-skip-model YOUR_MODEL

# 比赛或训练；手动任务可显式忽略长期累计费用
python contest_solver.py https://your-oj.example/contest/CONTEST_ID --workers 2
python contest_solver.py https://your-oj.example/training/TRAINING_ID --no-accum

# 定时监控 / 仅检查一次
python contest_daemon.py --interval 120 --domain system --start-page 2
python contest_daemon.py --once --ended-only --no-interactive

# 标程扫描 / 指定记录 / 补测试数据
python benchmark_solver.py --start-page 2
python benchmark_solver.py https://your-oj.example/record/RECORD_ID
python testdata_supplement.py https://your-oj.example/p/1000

# Web 管理与离线诊断
python webui.py --port 5000
python config_cli.py doctor --json
```

`--no-submit` 仍可能调用 AI；`--dry-run` 才是仅抓取题面。更多选项可运行对应脚本的 `--help`。

守护进程的控制台 / OJ 私信支持：

| 指令 | 作用 |
| --- | --- |
| `1000`、`p/1000`、题目 / 比赛 / 训练 URL | 触发求解 |
| `stats` / `统计`、`today` / `今日`、`recent` / `最近` | 查询统计与记录 |
| `pending` / `进行中`、`list` | 查询任务和已处理比赛 |
| `whitelist add/remove/list <UID>` | 管理白名单（超级用户） |
| `pushlist add/remove/list <UID>`、`push 消息` | 推送管理（超级用户） |
| `td 链接`、`bm`、`bm 链接` | 数据补充、标程扫描（超级用户） |
| `help`、`exit` / `quit` | 帮助；退出仅限控制台 |

配置 `msg_whitelist`、`msg_superuser`、`msg_push_list` 与 `msg_push_events` 控制权限和推送。初始化模板不自动授予任何用户管理权限。

## 费用、日志与常见问题

- `max_cost_per_problem` 默认 5，自动批量任务可用 `cost_accum_enable` 开启长期累计，记录在 `cost_accum.json`。手动单题默认忽略累计；手动批量可用 `--no-accum`。上限是调用前检查的软上限，最后一次请求可能超额。
- 难度判断、求解、题解修复和混淆费用纳入统计。费用来自模型配置的价格；未设置价格或供应商未返回 usage 时，费用不能作为真实账单依据。
- 控制台输出简要日志，`logs/` 保存详细日志；`dashboard.json` 保存统计，`processed_contests.json` 保存比赛处理状态。

| 现象 | 检查方式 |
| --- | --- |
| 没有扫描到比赛 | 默认起始页为 2；先试 `--start-page 1`，再检查域名、登录权限及处理记录 |
| API Key 缺失 | 运行 doctor；检查模型引用的环境变量，不会自动改用其他服务的密钥 |
| 修改 Web 配置不生效 | 环境变量优先于 JSON，模型独立配置优先于全局字段；容器环境变量修改后需重新创建 |
| Web 配置返回 403 | 在访问设置填写与 `.env` 相同的 `WEBUI_AUTH_TOKEN` |
| 回复达到长度上限 | 调高对应任务预算和模型上限，检查推理 token 是否占用输出额度 |
| 不支持某个请求参数 | 调整 provider、token_parameter、supports_reasoning_effort、thinking 或 stream_usage |
| 缺少本地工具链 | 本地安装 g++ / clang++，或使用内置 g++ 的 Docker 镜像 |
| Docker /data 权限错误 | 默认命名卷可自动初始化；自定义 bind mount 需要允许 UID/GID 10001 写入 |
| init 提示配置已存在 | 继续使用原文件，通过 Web / JSON 修改，或用 `--config` 选择新路径 |

## 开发与测试

```bash
python -m pytest -q
python -m pytest test_ai_client.py test_configuration.py test_token_budgets.py test_deployment.py test_model_catalog.py -q
```

测试使用模拟 AI/OJ，不需要真实凭据；缺少编译器时相关本地编译测试自动跳过。
GitHub Actions 覆盖 Windows / Linux、Python 3.10 / 3.12，并实际构建 Docker 镜像，验证编译器、配置持久化与 Web 启动。

| 模块 | 职责 |
| --- | --- |
| `ai_client.py` / `ai_protocol.py` | AI 请求、流式处理、供应商参数和计费 |
| `ai_context.py` / `prompt_defaults.py` / `prompts.json` | 修复上下文与提示词 |
| `config_manager.py` / `config_io.py` / `config_cli.py` | 配置合并、热重载、验证、脱敏、初始化 |
| `model_services.py` / `model_pricing.py` | 模型列表发现、地址归一化、价格目录缓存与汇率换算 |
| `contest_catalog.py` | 守护进程和标程扫描共用的比赛分页 |
| `runtime_paths.py` / `docker_entrypoint.py` | 运行数据路径与容器命令 |
| `oj_solver.py` / `solver_agent.py` / `local_judge.py` | 求解编排、候选方案和本地评测 |
| `editorial.py` / `oj_samples.py` | 题解校验与样例提取 |
| `oj_common.py` / `dashboard.py` / `msg_backend.py` | OJ 会话、统计和消息 |

旧的 `from oj_solver import AIClient` 继续兼容。许可证见 [LICENSE](LICENSE)。
