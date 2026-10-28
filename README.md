# HydroOJ AIbot — OJ 自动解题 & 题解发布系统

基于多种 AI 模型的 Hydro OJ 自动化工具，支持自动解题、题解发布、比赛监控、私信交互、测试数据补充、标程题解生成等功能。

## 功能概览

### 核心功能
- **AI 自动解题**: 8 级难度评估 + 三层模型路由（flash/pro/max），自动生成题解并发布
- **比赛守护进程**: 自动监控多个域的比赛，批量求解并推送通知
- **私信后端**: 白名单用户可通过 OJ 私信远程控制，支持实时指令交互
- **测试数据补充**: 自动为无测试数据的题目生成样例、std.cpp、data.cpp 并上传
- **标程题解生成**: 自动扫描比赛排行榜，检测标程用户满分题，AI 解读后发布

### 模型管理
- **多模型独立配置**: 每模型可设独立 base_url、api_key、max_tokens、reasoning_effort、定价
- **8 级难度系统**: 入门→普及−→普及/提高−→普及+/提高→提高→提高+/省选−→省选/NOI−→NOI/CTSC
- **自动限流切换**: 429 限流时自动降级模型（free→flash, max→pro→flash）
- **费用追踪**: 每模型独立定价（含峰谷价），单题累计费用上限（默认5元，跨调用/跨比赛检测长期累计）

### 多域支持
- 所有 OJ API 请求自动适配 system 和非 system 域
- 记录 URL、题目获取、题解发布、文件上传均支持跨域

---

## 快速开始

### 1. 环境配置

建议先创建单模型配置（需要 Python 3.10+）：

```bash
pip install -r requirements.txt
python config_cli.py init --model YOUR_MODEL --base-url https://your-api.example/v1 --oj-root https://your-oj.example --profile economy
# 填写生成的 .env：OJ_USERNAME、OJ_PASSWORD、AI_API_KEY
python config_cli.py doctor
python webui.py
```

`init` 不覆盖已有配置或 `.env`。`economy` 关闭单独的难度判断、使用两条候选思路，
保留本地验证、对拍和结构化题解；`balanced` 保留难度判断与三条候选。
供应商可用 `--provider openai|deepseek|zhipu|compatible` 指定（默认兼容端点）。
已有多模型配置可继续使用；下方是手动配置方式。

```bash
# 安装依赖
pip install -r requirements.txt

# 编辑 .env
OJ_ROOT=https://your-oj-instance.com
OJ_USERNAME=your_username
OJ_PASSWORD=your_password
AI_API_KEY=sk-xxx
AI_MODEL=deepseek-v4-pro
DEEPSEEK_API_KEY=sk-xxx
ZHIPU_API_KEY=your_zhipu_key
```

### 2. 单题求解

```bash
python oj_solver.py "https://your-oj-instance.com/p/1000"
python oj_solver.py 1000  # 简写
```

### 3. 比赛批量求解

```bash
python contest_solver.py "https://your-oj-instance.com/d/system/contest/xxx"

# 手动触发时可忽略单题金额累计（不检查累计上限）
python contest_solver.py "https://.../contest/xxx" --no-accum
```

### 4. 启动守护进程

```bash
python contest_daemon.py --interval 120
```

---

## 配置说明 (`config.json`)

```json
{
  "oj_root": "OJ 根地址",
  "ai_model": "默认模型名",
  "monitor_domains": ["system", "your_domain"],
  "monitor_interval": 120,
  "msg_whitelist": [2, 35],
  "msg_push_list": [2, 35],
  "msg_superuser": [2, 35],
  "benchmark_users": [2],
  "max_cost_per_problem": 5.0,
  "cost_accum_enable": true,
  "auto_supplement_testdata": false,
  "difficulty_detect_enable": true,
  "difficulty_skip_model": "",
  "model_router": {
    "tiers": { "flash": "...", "pro": "...", "max": "..." }
  },
  "models": {
    "glm-4.6v-flash": { "base_url": "...", "pricing": {...} },
    "deepseek-v4-pro": { "base_url": "...", "pricing": {...} },
    "deepseek-v4-flash": { "base_url": "...", "pricing": {...} },
    "glm-5.2": { "base_url": "...", "pricing": {...} }
  }
}
```

---

## 单题费用累计

`max_cost_per_problem`（默认 ¥5）按题目**长期累计**，持久化在 `cost_accum.json`（自动忽略）：

- **自动求解**（比赛/守护进程批量）: 每次求解结束后把费用累加到该题，跨调用生效——同一题被不同比赛反复检测时，金额持续累计，不会重复重置
- **达到上限后**立即停止后续 AI 调用，推送 💰 提示；该题被视为完成，不阻止比赛标记
- **手动触发忽略累计**: 命令行 `oj_solver.py` 以及守护进程交互/私信触发的求解，不检查累计限额
  - 手动批量可用 `contest_solver.py ... --no-accum` 显式忽略
- **全局关闭**: `config.json` 设 `"cost_accum_enable": false`（此时仅按单次会话限额）

> 注: 限额为软上限——最后一次 AI 调用在检查通过后执行，可能超额（超额幅度=单次调用费用）。

---

## 难度判断开关

默认会先让 AI 评估题目难度（8 级），再据此决定从哪一层模型开始解题。这一步可以整体关掉：

```json
{
  "difficulty_detect_enable": false,
  "difficulty_skip_model": "deepseek-v4-pro"
}
```

- **`difficulty_detect_enable: false`** — 跳过难度评估这次 AI 调用（少一次请求、少一份费用），题解里也不会再插入难度标签
- **`difficulty_skip_model`** — 关闭难度判断时用哪个模型：
  - 留空 → 沿用 `model_router` 的首个层级（默认 flash），失败后照常升级
  - 填 `model_router` 分层里的模型（如 pro/max 层的模型）→ 从该层开始，失败后继续向上升级
  - 填分层之外的模型 → 本次**只使用该模型**，不再走 flash→pro→max 分层
- 命令行可临时切换（不修改配置文件）：

```bash
python oj_solver.py 1178 --no-difficulty-detect
python oj_solver.py 1178 --difficulty-skip-model deepseek-v4-pro
```

> 比赛守护进程/私信触发的子进程同样会读取 `config.json` 中的这两项，也可用环境变量
> `OJ_DIFFICULTY_DETECT=0` / `OJ_DIFFICULTY_SKIP_MODEL=...` 覆盖。

---

## 守护进程指令

守护进程支持控制台和 OJ 私信两种交互方式：

### 求解
```
1316             — 按题号求解 (system 域)
p/1316           — 同上
完整URL          — 按链接求解题目/比赛/训练
```

### 查询
```
stats / 统计      — 汇总统计 (AC数/Token/费用)
today / 今日      — 今日统计
recent / 最近     — 最近记录
pending / 进行中  — 正在求解
list             — 已处理比赛
```

### 控制
```
help             — 帮助
exit / quit / q  — 退出 (仅控制台)
```

### 管理 (超级用户)
```
whitelist add/remove/list <UID>
pushlist add/remove/list <UID>
push <消息>
td <链接>          — 补充测试数据
bm               — 全扫标程题解
bm <链接>         — 指定范围标程题解
```

---

## 模块架构

```
ojai/
  oj_solver.py          — 单题求解 (OJClient + SolverOrchestrator)
  ai_client.py          — 统一 AI 客户端、流式响应、费用统计
  ai_protocol.py        — 供应商参数与 usage 归一化
  ai_context.py         — 保留完整题面/当前代码的修复上下文
  prompt_defaults.py    — 内置提示词兜底
  config_io.py          — 配置迁移、原子保存、校验、脱敏
  config_cli.py         — 初始化与离线诊断
  contest_solver.py     — 比赛/训练批量求解 (多线程)
  contest_daemon.py     — 守护进程 (监控 + 交互 + 私信)
  benchmark_solver.py   — 标程题解自动生成
  testdata_supplement.py — 测试数据补充
  model_router.py       — 8级难度评估 + 模型路由
  config_manager.py     — 配置管理 (热重载 + 补齐)
  dashboard.py          — 活动追踪 + 统计
  msg_backend.py        — OJ 私信后端
  webui.py              — Flask Web 界面
  onebot_server.py      — OneBot 协议适配
  oj_common.py          — 共享工具 (登录/会话/推送/日志)
```

---

## 求解流程

```
Phase 0: 免费模型快速尝试 (glm-4.6v-flash)
  ├─ 生成 → 提交 → 评测
  ├─ AC? → 完成
  └─ 429? → 自动切换 flash→pro→max

Phase 1: 难度判断 + 标签筛选   ← 可用 difficulty_detect_enable: false 跳过
  ├─ 8级难度评估
  └─ 100+标签库筛选候选 (≤10)
     跳过时: 直接用 difficulty_skip_model（未配置则用分层首个模型），不插入难度标签

Phase 2: 三层循环
  外层 (模型升级): flash → pro → max（指定了分层外的模型时只用该模型）
  中层 (换思路): generate ×2
  内层 (修正): submit → fix ×2

发布: 难度标签 + 标签 + 解题思路 + 代码 + 页脚(模型/Token/费用)
```

---

## 求解流程：多候选思路 + 反馈闭环

默认开启。**解题与写题解是两个独立阶段**，各自有独立的流程与校验。

### 解题阶段

```
每条层级下尝试 N 条互相独立的思路：

  思路① 出方案 → 写代码 → 本地编译 → 跑样例 → 随机对拍 → 提交评测
                                  ↑             ↑          ↓
                          编译报错/样例 diff    对拍反例    评测反馈
                                  └──── 带着反馈重写（R 次）────┘
  思路② 换一个算法角度（不是改上一版代码）→ 同上
  思路③ …

  全部结束后：AC 优先，其次取得分最高的那一版用于写题解
```

| 环节 | 作用 |
|------|------|
| 多候选思路 | `solve_candidates` 条**独立**思路；第 2 条起明确要求换算法（不同数据结构 / 不同转化 / 正难则反），避免在错误思路上打补丁 |
| 方案先行 | 先分析问题、定算法、列边界清单，再写代码（`agent_plan` / `agent_alternative_plan`） |
| 本地编译 | 编译错误立刻可见，并能把编译器原文回喂（而不是等评测返回一句 CE） |
| 样例校验 | 回喂**精确 diff**（输入 / 期望 / 实际），定位比 OJ 的「WA」快得多 |
| 随机对拍 | 额外要一份暴力解 + 数据生成器，本地对拍抓出边界错误（self-hack） |
| 评测反馈闭环 | 提交后的得分与用例分布继续回喂，作为该思路的修正依据 |
| 结构化记忆 | 每轮失败写入 journal，后续思路与修正都能看到「试过什么、错在哪」 |
| 预算控制 | 思路数 / 轮次 / 提交次数 / 总时长 / 费用多重上限，只用于防止失控 |

### 题解产出阶段（AC 之后）

不再直接发布求解时的原始输出，而是按固定结构重写一篇题解：

```
AC 代码 + 题目 + 评测数据 + 求解时的踩坑记录
   → 题意 / 思路 / 正确性说明 / 复杂度分析 / 实现要点与易错点 / 代码
   → 发布前校验（代码块必须与 AC 代码逐字一致、公式配对、结构完整、无对话痕迹）
   → 未通过则定向修复（只改被指出的问题，最多 editorial_fix_rounds 轮）
```

- **代码一致性是确定性保证**：正文里的代码块会被强制同步成真正 AC 的代码，不依赖模型自觉
- **易错点来自真实经历**：求解过程中踩过的坑（编译错误 / 样例 WA / 对拍反例）会作为素材写进「实现要点与易错点」
- **可以指定更强的模型写题解**：`editorial_model`（留空则沿用求解模型）

### 配置

```json
{
  "agent_enabled": true,
  "solve_candidates": 3,
  "solve_repairs": 2,
  "agent_max_steps": 12,
  "agent_max_submissions": 8,
  "agent_max_seconds": 900,
  "agent_stress_enable": true,
  "agent_stress_rounds": 30,
  "agent_run_timeout": 10,
  "editorial_enable": true,
  "editorial_model": "",
  "editorial_fix_rounds": 2
}
```

命令行：`--no-agent`（关闭本地验证，回到原三层循环）、`--agent-steps N`、
`--no-stress`（只做编译+样例）。环境变量：`OJ_AGENT_ENABLE` / `OJ_AGENT_MAX_STEPS` /
`OJ_AGENT_STRESS` / `OJ_SOLVE_CANDIDATES` / `OJ_SOLVE_REPAIRS` / `OJ_EDITORIAL_ENABLE` /
`OJ_EDITORIAL_MODEL`。

依赖与降级：C++ 需要本机有 `g++`/`clang++`（Python 题用 `python3`）。**检测不到工具链时会
自动回退到原来的三层循环**，题解产出流程不受影响（仍会重写题解）。

> ⚠️ 安全提示：本地验证会执行 AI 生成的代码。已限制单次运行超时、输出大小，
> POSIX 下还会限制地址空间/CPU 时间并在超时后杀进程组，但这**不是真正的沙箱**。
> 请把机器人跑在可丢弃的环境里，不要给它过高权限。

---

## 难度系统

| Lv | 标签 | 颜色 | 模型 |
|----|------|------|------|
| 1 | 入门 | `#FE4C61` | flash |
| 2 | 普及− | `#F39C11` | flash |
| 3 | 普及/提高− | `#FFC116` | pro |
| 4 | 普及+/提高 | `#52C41A` | pro |
| 5 | 提高 | `#13C2C2` | pro |
| 6 | 提高+/省选− | `#3498DB` | max |
| 7 | 省选/NOI− | `#9D3DCF` | max |
| 8 | NOI/NOI+/CTSC | `#0E1D69` | max |

---

## 日志系统

- 控制台: INFO 级别，简洁格式
- 文件: DEBUG 级别 (全量)，`logs/oj_YYYYMMDD.log`
- 自动轮转: 10MB × 5 份
- 启动时自动检查配置完整性

---

## 测试

```bash
python -m pytest -q                 # 全部离线测试（不需要 AI/OJ 凭据）
python -m pytest test_core.py -q    # 仅核心逻辑
python -m pytest test_optimizations.py -q   # 仅优化项回归
python -m pytest test_agent.py -q   # 仅 agent 求解层（缺 g++ 时自动跳过编译相关用例）
python -m pytest test_editorial.py -q       # 仅题解产出管线
```

---

## 环境变量

| 变量 | 说明 |
|------|------|
| `OJ_ROOT` | OJ 根地址 |
| `OJ_USERNAME` / `OJ_PASSWORD` | OJ 登录凭据 |
| `AI_API_KEY` | 全局默认 API Key |
| `AI_MODEL` | 默认模型名 |
| `DEEPSEEK_API_KEY` | DeepSeek 系列 API Key |
| `ZHIPU_API_KEY` | 智谱系列 API Key |
| `OJ_TD_MODEL` | 测试数据补充模型 |
| `OJ_BENCHMARK_MODEL` | 标程题解模型 |
| `OJ_DELAY_MODE` | 延迟模式开关 (≥20题自动) |
| `OJ_DIFFICULTY_DETECT` | 是否判断题目难度 (1/0，默认 1) |
| `OJ_DIFFICULTY_SKIP_MODEL` | 关闭难度判断时使用的模型 |


## AI 接口层（重构）

`ai_client.py` 统一生成、修正、流式与普通对话；`ai_protocol.py` 负责协议参数与用量归一化。
原来的 `from oj_solver import AIClient` 仍兼容。流式调用读取最后一个无 choices 的 usage 包；
缓存命中仅按缓存价计费，不再同时按普通输入价重复计算。达到输出上限的残缺代码不会作为成功结果提交。

模型配置支持以下可选项（均位于 `models.<模型名>`）：

- `provider`: `auto` / `openai` / `deepseek` / `zhipu` / `compatible`。代理地址建议显式设置；
  自动识别仅匹配官方主机名，未知兼容端点不附带专有推理参数。
- `token_parameter`: `max_tokens` 或 `max_completion_tokens`，默认随 provider 选择。
- `supports_reasoning_effort`: 是否传推理强度。OpenAI 与代理模型需按实际能力显式设为 `true`；其他情况下不猜测模型能力。DeepSeek 保留旧配置的默认行为。
- `thinking`: `enabled` / `disabled`，控制 DeepSeek / 智谱风格的 thinking 字段。
- `stream_usage`: 默认 `true`；不接受 `stream_options` 的网关可设为 `false`，此时可能无法统计流式费用。
- `extra_body`: 网关扩展字段；不得覆盖模型、消息、流式开关或 token 上限。
- `timeout` / `max_retries`: 默认 180 秒 / 2 次，交由 SDK 处理可重试错误。

请求参数与流式用量处理依据 [Chat Completions 官方接口文档](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)。
模型能力、价格与输出限制需按实际服务配置；示例配置中的价格不是实时价格查询。


## 输出预算与配置诊断

新增 `ai_task_tokens`，默认输出上限如下，实际请求取任务预算与模型 `max_tokens` 的较小值：

| 任务 | 默认上限 |
| --- | ---: |
| 通用 / 求解 | 16384 |
| 方案分析 | 4096 |
| 对拍素材 | 8192 |
| 难度评估 | 1024 |
| 题解写作与修复 | 8192 |

预算包含接口计入输出的推理 token；复杂题需要时请提高对应预算。减少输出上限不等于保证减少
实际消费，节省比例取决于题目、模型和重试次数。首轮已经 AC 后会跳过难度请求；修复历史默认
仅保留完整题面、当前代码、最近失败摘要和最新反馈，可用 `compact_history: false` 恢复完整历史。
题解模型只输出讲解，AC 代码由程序插入，修复题解时也只发送一份完整代码。
难度评估、题解生成/修复与混淆的费用现在都会纳入单题统计。

```bash
python config_cli.py doctor --json
python config_cli.py --config path/to/config.json doctor
```

诊断只检查本地配置、环境变量引用与请求参数，不发付费请求，不代表服务端连接成功。
配置优先级仍为：命令行 / 运行时覆盖 > 环境变量 > JSON > 默认值。覆盖在热重载后保留；
JSON 解析失败时保留上一次有效配置。密钥推荐 `env:MY_KEY` 或 `${MY_KEY}` 引用，
显式引用缺失时会报缺失，不会把另一个服务的全局密钥发过去。配置文件保存保留扩展字段。

WebUI 的配置页使用与求解器一致的扁平结构，兼容读取旧的 `oj` / `ai` / `code` / `monitor`
分组结构。保存前验证数值和模型参数，GET 不返回密码或模型密钥原文。使用 `--auth-token`
时，先在配置页的「访问设置」输入令牌，再刷新。全局字段与模型独立字段的优先级不变；
模型表仍可直接编辑 JSON，页面提供全局设置与各任务预算。

GitHub Actions 会在 Windows / Linux、Python 3.10 / 3.12 上运行离线回归测试。
