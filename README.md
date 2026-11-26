# HydroOJ AIbot

**当前版本：2.2.0**

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
| Web 面板 | `webui.py` | Token 登录、配置、模型供应商、统计与运行日志 |
| OneBot 接入 | `onebot_server.py` | 接收消息指令并调用求解脚本 |

所有命令请在项目目录运行。Docker 已设置独立的数据目录，无需手动管理工作路径。

## 项目结构

代码按领域拆分在 `hydroai/` 包中，仓库根目录只保留兼容入口脚本：

```text
config/     用户配置：config.json、.env 及模板
hydroai/
  config/   配置加载、校验、CLI、运行时路径
  ai/       AI 客户端、协议、路由、定价、提示词
  oj/       OJ 站点交互、样例解析、本地评测
  solve/    单题/比赛求解、Agent、题解、测试数据
  monitor/  比赛守护、私信、OneBot
  usage/    usage.db 逐请求统计与订阅额度
  web/      Web 面板、仪表盘、模板
tests/      离线测试（由 pytest.ini 指定 pythonpath）
```

`oj_solver.py`、`contest_solver.py`、`contest_daemon.py`、`webui.py`、`config_cli.py` 等根目录脚本只是入口包装，原有命令保持不变。本地默认从 `config/` 读取 `config.json` 与 `.env`，并兼容旧根目录文件；`model_prices.json` 跟随配置文件目录。运行数据（`dashboard.json`、`usage.db`、`daemon.pid`、`cost_accum.json`、Cookie、日志）仍写入 `OJ_DATA_DIR` 或当前工作目录。Docker 中 `OJ_DATA_DIR=/data`，`config.json` 与运行数据在持久化卷内；`.env` 由宿主机 `config/.env` 注入。

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
python config_cli.py --version
python config_cli.py init --model YOUR_MODEL --base-url https://your-api.example/v1 --oj-root https://your-oj.example --profile economy
```

`init` 创建 `config/config.json` 和空白 `config/.env`，不会覆盖已有文件。在 `config/.env` 填写：

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

已有配置可直接保留，也可参考 [完整配置示例](config/config.example.json)。指定其他配置文件执行初始化或诊断：

```bash
python config_cli.py --config path/to/config.json doctor --json
```

## Docker / Compose 部署

需要能运行 Linux 容器的 Docker Engine / Docker Desktop，以及 Docker Compose v2。仓库提供 `Dockerfile`、`compose.yaml`、`.dockerignore` 和 [config/.env.docker.example](config/.env.docker.example)。镜像基于 Python 3.12，包含 `g++`，以 UID/GID **10001:10001** 运行。

### 首次启动

在仓库目录复制环境变量模板：

```bash
# Linux / macOS
cp config/.env.docker.example config/.env
# Windows PowerShell
Copy-Item config/.env.docker.example config/.env
```

填写 `config/.env` 的 `OJ_USERNAME`、`OJ_PASSWORD`、`AI_API_KEY`、`WEBUI_AUTH_TOKEN`。
`WEBUI_AUTH_TOKEN` 请设置为自行生成的随机字符串；Compose 会拒绝使用空令牌启动。
已有 `config/.env` 时保留它并补充 `WEBUI_AUTH_TOKEN`，不要用模板覆盖凭据。

```bash
docker compose --env-file config/.env build
docker compose --env-file config/.env run --rm cli init --model YOUR_MODEL --base-url https://your-api.example/v1 --oj-root https://your-oj.example --profile economy
docker compose --env-file config/.env run --rm cli doctor
docker compose --env-file config/.env up -d web
```

打开 [http://127.0.0.1:5000](http://127.0.0.1:5000) 会强制跳转登录页，输入 `config/.env` 的 `WEBUI_AUTH_TOKEN` 后才能使用 Web。登录使用 HttpOnly 会话 Cookie；配置页的「脚本访问令牌」只用于保存 `X-Auth-Token` 供脚本调试。
`config/.env` 的 `WEBUI_PORT` 可修改宿主机端口，例如 `WEBUI_PORT=8080`。可选设置 `WEBUI_SECRET_KEY`（固定会话密钥，重启后仍保持登录）和 `WEBUI_COOKIE_SECURE=1`（HTTPS 反向代理时启用 Secure Cookie）。

Web 与守护进程运行在**同一个容器**中，不再有单独的 daemon 服务。Web 默认随启动自动拉起守护进程（`WEBUI_AUTOSTART_DAEMON=1`），可在「比赛守护」卡片停止、重启或执行一次检查。

只希望手动启动守护进程时，在 `config/.env` 设置：

```dotenv
WEBUI_AUTOSTART_DAEMON=0
```

守护参数通过 `WEBUI_DAEMON_ARGS` 传入，例如只处理已结束的比赛：

```dotenv
WEBUI_DAEMON_ARGS=--ended-only
```

### 容器命令

`cli` 仅用于一次性任务，不会随普通 `up` 自动启动：

```bash
docker compose --env-file config/.env run --rm cli doctor --json
docker compose --env-file config/.env run --rm cli solve https://your-oj.example/p/1000
docker compose --env-file config/.env run --rm cli contest https://your-oj.example/contest/CONTEST_ID --workers 2
docker compose --env-file config/.env run --rm cli benchmark --start-page 2
docker compose --env-file config/.env run --rm cli testdata https://your-oj.example/p/1000
docker compose --env-file config/.env run --rm cli daemon --once --no-msg-backend --start-page 1
docker compose --env-file config/.env run --rm cli usage --days 30
docker compose --env-file config/.env run --rm cli quota --json
```

容器入口还支持 `web`、`onebot`、`config`、`usage`、`quota` 和 `python`。例如：

```bash
docker compose --env-file config/.env run --rm cli config --help
docker compose --env-file config/.env run --rm cli solve --help
```

### GitHub Actions 构建与发布

`.github/workflows/docker.yml` 在以下事件运行：

- `push` 到 `main` 或 `v*` 标签
- 任意 `pull_request`（只构建和冒烟测试，不推送）
- 手动 `workflow_dispatch`

流程：构建镜像 → 运行 `docker-smoke.sh`（初始化、诊断、`usage`/`quota` 命令、Web `/status`、`/api/usage/summary`、`/api/quota`、配置持久化）。PR 和手动触发只构建与冒烟测试；`push` 到 `main` 或标签时推送到 GitHub Container Registry：

```text
ghcr.io/shrimp-211/hydrooj-aibot:latest
ghcr.io/shrimp-211/hydrooj-aibot:v2.2.0
ghcr.io/shrimp-211/hydrooj-aibot:<commit-sha>
ghcr.io/shrimp-211/hydrooj-aibot:<tag>      # push v* 标签时
```

使用已发布镜像（无需本地构建）：

```bash
echo "$GITHUB_TOKEN" | docker login ghcr.io -u YOUR_GITHUB_USER --password-stdin
docker pull ghcr.io/shrimp-211/hydrooj-aibot:latest
IMAGE=ghcr.io/shrimp-211/hydrooj-aibot:latest docker compose --env-file config/.env up -d web
```

`compose.yaml` 的 `image` 支持 `IMAGE` 变量，默认仍是本地构建的 `hydrooj-aibot:local`。首次发布后请在仓库的 Packages 设置中确认镜像可见性；公开镜像可以直接匿名拉取。工作流已声明 `packages: write`，无需额外 secret。

直接使用 Docker 时，使用同一命名卷并传入 `config/.env`：

```bash
docker build -t hydrooj-aibot:local .
docker run --rm --init --env-file config/.env -v hydrooj-data:/data hydrooj-aibot:local init --model YOUR_MODEL --base-url https://your-api.example/v1 --oj-root https://your-oj.example
docker run --rm --init --env-file config/.env -v hydrooj-data:/data hydrooj-aibot:local doctor
```

直接 Docker 示例与 Compose 创建的卷相互独立；请固定使用同一种部署方式或明确迁移数据。

### 数据、升级与备份

- `/app`：镜像中的代码与提示词；升级镜像会更新它们。
- `/tmp`：可执行的临时内存文件系统，供本地编译、样例验证和数据生成使用，容器停止后清空。
- `/data`：命名卷，保存 `config.json`、`usage.db` 逐请求统计、`daemon.pid`、费用统计、处理记录、Cookie、日志、本地验证目录和生成数据。
- 宿主机 `config/.env`：由 Compose 注入环境变量，不会复制到镜像。变量优先于卷内 JSON 配置；修改后需要重新创建服务。

```bash
# 查看状态和日志（Web 与守护进程在同一容器）
docker compose --env-file config/.env ps
docker compose --env-file config/.env logs -f web

# 拉取更新、重建并启动
git pull --ff-only
docker compose --env-file config/.env build
docker compose --env-file config/.env up -d --force-recreate

# 停止服务，保留数据卷
docker compose --env-file config/.env down
```

备份时先在 Web「比赛守护」停止守护，或从容器内停止，再复制 `/data`：

```bash
docker compose --env-file config/.env exec web python -c "from hydroai.monitor import daemon_control; daemon_control.stop()"
docker compose --env-file config/.env cp web:/data/. ./backups/
```

恢复到现有 web 容器时：

```bash
docker compose --env-file config/.env cp ./backups/. web:/data/
docker compose --env-file config/.env run --rm --user 0 --cap-add CHOWN --entrypoint chown cli -R 10001:10001 /data
docker compose --env-file config/.env restart web
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

三种配置方式，优先级为 **命令行 > 环境变量 > config/config.json（Docker 为 /data/config.json） > 默认 2**：

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
docker compose --env-file config/.env run --rm cli daemon --once --start-page 1
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
docker compose --env-file config/.env run --rm cli config models --json
```

API 地址支持粘贴完整的 `/chat/completions` 或 `/models` 地址，程序会还原基础地址，保留自定义路径前缀。
仅在根地址的 `/models` 返回 404/405 时尝试 `/v1/models`。返回的 `base_url` 是查询成功的基础地址。
鉴权失败不会继续探测其他端点；请求不跟随重定向，密钥不会被转发给另一个站点。
模型配置可指定同源的 `models_url`；列表支持 `data` / `models` 数组。

新增连接只保存密钥环境变量引用，例如：

```bash
python config_cli.py add-model fast --model-id YOUR_API_MODEL_ID --base-url https://your-api.example/v1 --api-key-env MY_PROVIDER_KEY --provider compatible
```

先在 `config/.env` 设置 `MY_PROVIDER_KEY`；Docker 需要将新增变量注入容器后重新创建服务。
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
docker compose --env-file config/.env run --rm cli config prices --refresh
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

### 用量与订阅额度

参考 cc-switch 的 Usage / Quotas & Balances 设计：每次 AI 请求写入 `usage.db`（SQLite），记录模型、供应商、任务、题目、输入/输出/缓存 token、费用、耗时与输出速度；额度在本地用量之上计算，也可查询供应商余额。

```bash
python config_cli.py usage --days 30   # 汇总、按模型分布、今日
python config_cli.py usage --json
python config_cli.py usage --prune     # 按 usage_retention_days 立即清理旧明细
python config_cli.py quota             # 额度与余额
python config_cli.py quota --refresh   # 强制刷新远端余额（忽略缓存）
```

Web 面板的「用量与额度」页展示概览卡片、额度进度与重置倒计时、每日趋势、全年热力图、按模型/供应商/任务分布和最近请求。配置页底部提供「高级配置（JSON）」，可编辑包括 `quotas` 在内的全部字段；密钥不会回显，省略 `api_key` 会保留原值，设为 `null` 清除，`*_configured` 标记在保存时自动忽略。私信/控制台可用 `usage` / `用量`、`quota` / `额度`。

Web API 鉴权：Web 页面先通过登录页换取 HttpOnly 会话 Cookie；脚本调用需携带 `X-Auth-Token`。`/config`、`/run/*`、`/daemon/*`、`/api/usage/*`、`/api/quota`、`/api/stats`、`/api/today`、`/api/history`、`/history` 均要求鉴权，未设置 `WEBUI_AUTH_TOKEN` 时 Web 进程拒绝启动。`/status` 保持公开供容器健康检查，`/api/version` 公开返回版本号。`/stream` 由前端先申请 60 秒短时票据再连接，避免把长期 Token 放进 URL。

```json
{
  "usage_stats_enable": true,
  "usage_retention_days": 365,
  "quotas": {
    "deepseek": {
      "models": ["deepseek-v4-pro", "deepseek-v4-flash"],
      "kind": "balance",
      "source": "deepseek",
      "currency": "CNY",
      "low_threshold": 5.0,
      "cache_seconds": 300
    },
    "zhipu-coding": {
      "models": ["glm-5.2"],
      "kind": "window",
      "unit": "cost",
      "limit": 50.0,
      "period": "5h",
      "low_ratio": 0.1
    }
  }
}
```

- `kind: window`：按 `models` 过滤本地用量，支持 `period: 5h|daily|weekly|monthly` 或滚动 `window_hours`，`unit: cost|tokens|requests`，给出剩余额度与重置倒计时。窗口是本地近似，官方限流以服务商为准。
- `kind: balance`：`source: manual` 使用手工 `balance`；内置 `deepseek`、`stepfun`、`siliconflow`（国内 `.cn` / 国际 `.com`）、`openrouter`、`novita`、`mimo`（小米）、`zhipu`；`newapi` 与 `sub2api` 适配常见中转站；`generic` 用 `endpoint` + `json_path`（可选 `used_json_path` / `total_json_path`）适配任意 JSON 接口。
- `kind: plan`：直接查询供应商套餐额度，返回 5 小时 / 每周 / 每月 / credits 多档百分比与重置时间。`source` 支持 `kimi`、`zhipu_plan`、`minimax`、`opencode_go`、`ollama`、`mimo_plan`。智谱、MiniMax、Kimi 的国内/国际端点会按 `base_url` 自动选择；请使用对应的套餐 API Key（例如 MiMo Token Plan 的 `tp-...`）。
- New API：`base_url` 填站点根地址，`api_key` 填访问令牌，`headers` 加 `{"New-Api-User": "<用户 ID>"}`。Sub2API：`base_url` 填站点地址，`api_key` 填供应商 Key；两者都按 cc-switch 的模板换算余额。
- 余额/套餐查询使用额度项的 `api_key`（支持 `env:NAME` / `${NAME}`），否则使用 `models` 中第一个模型的密钥；未指定模型时才回退全局密钥，避免串用其他供应商凭据。查询不跟随重定向，错误信息不包含密钥。
- 远端结果缓存 `cache_seconds`（默认 300 秒）；失败时回退到最近一次有效快照并标记过期。费用依赖定价，未知价格会明确标注，不按免费处理。
- `usage_retention_days` 控制明细保留天数；AI 客户端每天最多自动清理一次，也可用 `python config_cli.py usage --prune` 立即执行。数据库只追加请求记录，单次写入失败不会中断 AI 调用。
- 官方 OAuth 订阅（Claude / Codex / Gemini / Grok 的账号配额）需要读取本机 CLI 的 OAuth 凭据，本项目未接入；火山方舟（需要 AK/SK 签名）与 Command Code 的多接口组合也暂未实现。这些供应商可继续使用 `window` 本地窗口或 `generic` 余额模板。

### 预置供应商与一键切换

Web 的「模型供应商」页采用 cc-switch 风格布局：左侧显示当前模型与已接入连接，右侧是按“模型厂商 / 聚合平台 / 云服务商 / 本地部署 / 中转自建 / 账号登录”分类的可搜索预设卡片。

- 内置 50+ 预设，覆盖 OpenAI、Anthropic 兼容端点、Gemini 兼容端点、DeepSeek、智谱、Kimi、MiniMax、Qwen、千帆、混元、火山方舟、StepFun、MiMo、OpenRouter、SiliconFlow、Novita、Groq、Mistral、xAI、Ollama、LM Studio、New API、Sub2API 等。
- 选择预设后填写配置名称、模型 ID、Base URL 与 API Key 环境变量；密钥只保存为 `env:NAME` 引用，实际值放在 `config/.env`。
- 「切换」会把 `ai_model` 与 `model_router.tiers` 全部指向该模型（单模型路由）；「仅设为默认」只改 `ai_model`。
- 预置模型 ID 是示例值，以供应商控制台为准，可随时修改。
- Claude Code / ChatGPT Codex / Gemini / Copilot / Grok 订阅属于 OAuth 账号登录，本项目使用 API Key + Chat Completions，不接入这些账号方式；对应卡片仅作分类展示。
- API：`GET /api/providers` 返回预设与已接入模型，`POST /api/models/switch` 切换当前模型。

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

### AI 错误处理、额度与通知

不在任务开始前主动消耗 token 探测。只有真实 AI 请求返回鉴权/限流/网络/服务端错误时，才检查是否所有 AI 都不可用：

1. 优先查询已配置的 `quotas`（`kind=balance/plan`，即余额/套餐额度）；任一渠道仍有剩余，认为 AI 可用，任务继续按原逻辑重试或换模型。
2. 没有可用的额度信息时，才回退到一次极小请求探测（最多 12 个模型）。
3. 全部不可用时，单题、比赛、标程、测试数据任务中止；带 `OJ_REQUESTER` 的私信回复请求者，守护/比赛模式向 `msg_push_list` 推送 `ai_unavailable`；Web 实时控制台显示错误。
4. 同一错误类型（`auth` / `rate_limit` / `network` / `server` / `quota` 等组合）在配置未变更前只通知一次；配置 mtime 变化后重新允许通知。通知账本存于 `usage.db` 的 meta 表，按“配置版本 + 错误类型 + 通道”去重。

```json
{
  "ai_health_check_enable": true,
  "ai_health_check_ttl": 300,
  "ai_health_check_timeout": 15,
  "msg_push_events": {"ai_unavailable": true}
}
```

`ttl=0` 表示每次触发都重新检查；失败原因只包含额度项/模型名、异常类型和 HTTP 状态，不输出 Key、URL 或响应正文。`--dry-run` 不调用 AI，不会触发。

Web 的「比赛守护」卡片可启动、停止或执行一次检查。守护进程把 PID 写入数据目录的 `daemon.pid`，因此 Web 重启后仍能识别并停止此前启动的守护进程；`/status` 同时返回 `daemon_running` 与 `daemon_pid`。启动失败会在任务消息和实时控制台中显示退出码。Docker 中 Web 与守护进程共用一个容器，默认自动启动守护进程。

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
python webui.py --port 5000 --auth-token YOUR_WEB_TOKEN
python config_cli.py doctor --json
```

WebUI 强制 Token 登录：启动前必须设置 `WEBUI_AUTH_TOKEN` 或传入 `--auth-token`，否则进程拒绝启动；打开页面后先输入 Token。Docker 部署由 Compose 强制要求 `config/.env` 中的 `WEBUI_AUTH_TOKEN`。

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
| 无法登录 / API 返回 403 | 在登录页输入 `config/.env` 的 `WEBUI_AUTH_TOKEN`；脚本请求携带 `X-Auth-Token`；未配置 Token 时 Web 会拒绝启动 |
| 回复达到长度上限 | 调高对应任务预算和模型上限，检查推理 token 是否占用输出额度 |
| 不支持某个请求参数 | 调整 provider、token_parameter、supports_reasoning_effort、thinking 或 stream_usage |
| 缺少本地工具链 | 本地安装 g++ / clang++，或使用内置 g++ 的 Docker 镜像 |
| Docker /data 权限错误 | 默认命名卷可自动初始化；自定义 bind mount 需要允许 UID/GID 10001 写入 |
| init 提示配置已存在 | 继续使用原文件，通过 Web / JSON 修改，或用 `--config` 选择新路径 |

## 开发与测试

```bash
python -m pytest -q
python -m pytest tests/test_ai_client.py tests/test_configuration.py tests/test_token_budgets.py -q
```

`pytest.ini` 已设置 `testpaths = tests` 和 `pythonpath = .`。测试使用模拟 AI/OJ，不需要真实凭据；缺少编译器时相关本地编译测试自动跳过。
GitHub Actions 覆盖 Windows / Linux、Python 3.10 / 3.12，并实际构建 Docker 镜像，验证编译器、配置持久化与 Web 启动。

| 模块 | 职责 |
| --- | --- |
| `hydroai/ai/client.py` / `protocol.py` | AI 请求、流式处理、供应商参数和计费 |
| `hydroai/ai/context.py` / `prompts.py` / `prompts.json` | 修复上下文与提示词 |
| `hydroai/config/manager.py` / `io.py` / `cli.py` | 配置合并、热重载、验证、脱敏、初始化 |
| `hydroai/ai/services.py` / `pricing.py` | 模型列表发现、地址归一化、价格目录缓存与汇率换算 |
| `hydroai/oj/catalog.py` | 守护进程和标程扫描共用的比赛分页 |
| `hydroai/config/paths.py` / `hydroai/cli/entrypoint.py` | 运行数据路径与容器命令 |
| `hydroai/solve/solver.py` / `agent.py` / `hydroai/oj/judge.py` | 求解编排、候选方案和本地评测 |
| `hydroai/solve/editorial.py` / `hydroai/oj/samples.py` | 题解校验与样例提取 |
| `hydroai/oj/common.py` / `hydroai/web/dashboard.py` / `hydroai/monitor/messaging.py` | OJ 会话、仪表盘和消息 |
| `hydroai/usage/store.py` / `quota.py` | 逐请求 token/费用账本、订阅额度与余额 |
| `hydroai/web/app.py` / `templates/` | Web 面板与用量/额度 API |

根目录的入口脚本只负责调用 `hydroai.*` 的 `main()`。内部模块请直接 `from hydroai.<包>.<模块> import ...`。许可证见 [LICENSE](LICENSE)。
