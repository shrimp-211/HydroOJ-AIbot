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

```bash
# 安装依赖
pip install openai requests python-dotenv

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
  oj_solver.py          — 单题求解 (AIClient + OJClient + SolverOrchestrator)
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

Phase 1: 难度判断 + 标签筛选
  ├─ 8级难度评估
  └─ 100+标签库筛选候选 (≤10)

Phase 2: 三层循环
  外层 (模型升级): flash → pro → max
  中层 (换思路): generate ×2
  内层 (修正): submit → fix ×2

发布: 难度标签 + 标签 + 解题思路 + 代码 + 页脚(模型/Token/费用)
```

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
python -m pytest test_core.py -q    # 33 个单元测试
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
