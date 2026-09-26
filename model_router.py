#!/usr/bin/env python3
"""多模型路由 — 8 级难度评估 + 自动选择模型和思考深度"""

import os, json, logging
import re

log = logging.getLogger(__name__)

# ═══════════════════════════ Model Tier Config ═══════════════════════════
class ModelTier:
    def __init__(self, name: str, model: str, thinking_levels: list[str]):
        self.name = name
        self.model = model
        self.thinking = thinking_levels

    def current_thinking(self, level: int = 0) -> str:
        # level 可能为负或超出列表长度（重试次数 > 配置的思考档位时），
        # 越界一律返回空串，让调用方退回模型默认推理强度。
        if not isinstance(level, int) or level < 0 or level >= len(self.thinking):
            return ""
        return self.thinking[level]


class ModelRouter:
    """多模型路由器。8级难度 → flash(1-2) / pro(3-5) / max(6-8)"""

    def __init__(self, config_manager=None):
        self.tiers: list[ModelTier] = []
        self.config = config_manager
        self._load()

    def _load(self):
        if self.config:
            for tier_name in ("flash", "pro", "max"):
                model = self.config.get_tier_model(tier_name)
                if not model:
                    continue
                thinking = self.config.get_tier_thinking(tier_name)
                self.tiers.append(ModelTier(tier_name, model, thinking))
        if not self.tiers:
            for key in ("AI_MODEL_FLASH", "AI_MODEL_PRO", "AI_MODEL_MAX"):
                model = os.environ.get(key, "")
                if not model: continue
                name = key.replace("AI_MODEL_", "").lower()
                thinking_raw = os.environ.get(f"AI_THINKING_{name.upper()}", "")
                thinking = [t.strip() for t in thinking_raw.split(",") if t.strip()] if thinking_raw else []
                self.tiers.append(ModelTier(name, model, thinking))
        if not self.tiers:
            model = os.environ.get("AI_MODEL", "glm-5.2")
            self.tiers.append(ModelTier("default", model, []))

    @property
    def flash(self) -> ModelTier | None:
        return self._tier("flash")

    @property
    def default(self) -> ModelTier:
        return self.tiers[0] if self.tiers else ModelTier("default", "glm-5.2", [])

    def _tier(self, name: str) -> ModelTier | None:
        for t in self.tiers:
            if t.name == name: return t
        return None

    # ── 8 级难度标签 ──
    DIFFICULTY_LABELS = {
        1: ("入门", "#FE4C61"),
        2: ("普及−", "#F39C11"),
        3: ("普及/提高−", "#FFC116"),
        4: ("普及+/提高", "#52C41A"),
        5: ("提高", "#13C2C2"),
        6: ("提高+/省选−", "#3498DB"),
        7: ("省选/NOI−", "#9D3DCF"),
        8: ("NOI/NOI+/CTSC", "#0E1D69"),
    }

    @classmethod
    def difficulty_tag(cls, level: int) -> str:
        label, color = cls.DIFFICULTY_LABELS.get(level, ("?", "#999"))
        return f'$\\colorbox{{{color}}}{{\\textcolor{{FFFFFF}}{{{label}}}}}$'

    # ── 难度判断 prompt ──
    DIFFICULTY_PROMPT = """请判断以下题目的难度等级，并从标签库中筛选最相关的标签。

等级标准(1-8)：
1=入门(基本语法,直接模拟) 2=普及−(枚举/贪心,O(n²)) 3=普及/提高−(递归/搜索,状态划分)
4=普及+/提高(综合策略,数据关联,适度优化) 5=提高(复杂建模,多步协同)
6=提高+/省选−(高级数据结构,多模块) 7=省选/NOI−(理论模型,高级优化)
8=NOI/NOI+/CTSC(前沿思路,顶级创造力)

## 标签库
模拟,枚举,贪心,递推,递归,二分法,倍增,分治,构造,前缀和,差分,离散化,扫描线,分块,离线处理,高精度
冒泡排序,选择排序,插入排序,计数排序,归并排序,快速排序,堆排序,桶排序,基数排序
栈,队列,链表,单调栈,单调队列,优先队列,并查集,哈希表,ST表,树状数组,线段树,平衡树,可持久化数据结构,树链剖分,LCT
DFS,BFS,FloodFill,记忆化搜索,双向BFS,迭代加深,启发式搜索,A*
最短路(Dijkstra/SPFA/BellmanFord),Floyd,最小生成树,拓扑排序,欧拉路,强连通分量,割点割边,二分图,网络流,2-SAT
LCA,树的直径,树的重心,树上差分,DFS序,基环树,虚树,最小树形图
一维DP,背包DP,区间DP,树形DP,状压DP,数位DP,DP优化(斜率/四边形不等式)
KMP,Manacher,AC自动机,后缀数组,后缀自动机,扩展KMP
质数/合数,唯一分解,GCD/欧几里得,素数筛,模逆元,CRT,欧拉定理,费马小定理,原根,BSGS,FFT/NTT
组合数学,排列组合,容斥原理,卡特兰数,斯特林数,莫比乌斯反演,生成函数,群论,Burnside/Pólya
矩阵运算,高斯消元,线性基,向量空间,凸包,半平面交,计算几何
博弈论(Nim/SG函数),概率期望,信息论

## 输出格式（严格）
难度:<1-8的数字>
标签:<逗号分隔,最多10个>

题目：
{content}

仅输出难度和标签，不要其他内容："""

    @classmethod
    def parse_diff_and_tags(cls, text: str) -> tuple:
        """解析难度判断结果，返回 (difficulty: int, tags: list[str])

        兼容模型的实际输出偏差：
        - 全角冒号（`难度：4`）与 Markdown 包裹（`**难度**: 4`）
        - 多位数难度（`难度: 10` 曾因逐字符取数字被解析成 1）
        - 标签写成 `[标签: a, b]` / `标签：a，b`（全角逗号、方括号）
        """
        diff = 3
        tags: list[str] = []
        got_diff = False
        clean = text.replace("**", "").replace("`", "")
        for line in clean.split("\n"):
            line = line.strip()
            if not line:
                continue
            # 难度与标签可能出现在同一行（模型自行合并输出），因此分别判断
            if not got_diff:
                m_diff = re.search(r"(?:难度|difficulty|level)\s*[:：]?\s*(\d+)", line, re.I)
                if m_diff is None and "难度" in line:
                    m_diff = re.search(r"(\d+)", line)
                if m_diff:
                    diff = max(1, min(8, int(m_diff.group(1))))
                    got_diff = True
            if "标签" in line or "tag" in line.lower():
                # 取「标签」/「tag」关键字之后的部分（同一行还可能带难度前缀，
                # 形如「难度:3 标签: 模拟」，所以不能只按首个冒号切分）
                m_tag = re.search(r"(?:标签|tag)\s*[:：]?\s*(.*)$", line, re.I)
                tag_part = m_tag.group(1) if m_tag else line
                tag_part = tag_part.strip().strip("[]【】").strip()
                candidates = [t.strip() for t in re.split(r"[,，、;；]", tag_part) if t.strip()]
                if candidates and not tags:
                    # 去重保序 + 最多 10 个
                    seen, uniq = set(), []
                    for t in candidates:
                        if t not in seen:
                            seen.add(t)
                            uniq.append(t)
                    tags = uniq[:10]
        return diff, tags

    # ── 标签筛选 prompt（求解时最终确定 ≤5 个）──
    TAG_FILTER_PROMPT = """从以下候选标签中选出最能描述本题的标签（最多5个，按相关性排序）。
候选标签: {candidates}
仅回复标签，逗号分隔，不要其他内容。"""

    @classmethod
    def tags_tag(cls, tags: list[str]) -> str:
        """标签行: tag: xxx, xxx"""
        if not tags:
            return ""
        return f"tag: {', '.join(tags[:5])}"

    # ── 求解策略 ──
    @staticmethod
    def tier_index_for_difficulty(difficulty: int) -> int:
        """8 级难度 → 起始层级索引（1-2→flash, 3-5→pro, 6-8→max）。

        难度到层级的映射只在这里定义一份，求解读解流程等调用方复用它，
        避免同一规则在多处各写一遍后逐渐漂移。
        """
        if difficulty <= 2:
            return 0
        if difficulty <= 5:
            return 1
        return 2

    def clamp_index(self, idx: int) -> int:
        """把层级索引夹到有效范围（tiers 可能只有 1~2 层）。"""
        if not self.tiers:
            return 0
        return max(0, min(idx, len(self.tiers) - 1))

    def tier_index_for_model(self, model_name: str) -> int | None:
        """查找某个模型所在的层级索引，不在分层里返回 None。

        用于「关闭难度判断 + 指定专用模型」的场景：如果该模型本身就是
        model_router 的一层，就从这一层开始（保留原升级顺序，如 pro→max）。
        """
        if not model_name:
            return None
        for i, tier in enumerate(self.tiers):
            if tier.model == model_name:
                return i
        return None

    def solve_strategy(self, difficulty: int, attempt: int) -> tuple[str, str]:
        """8 级难度映射: 1-2→flash, 3-5→pro, 6-8→max"""
        if not self.tiers:
            return self.default.model, ""

        idx = self.clamp_index(self.tier_index_for_difficulty(difficulty) + attempt)
        tier = self.tiers[idx]
        thinking = tier.current_thinking(attempt)
        log.info("[路由] 难度%d 尝试%d → %s/%s", difficulty, attempt, tier.model, thinking or "无")
        return tier.model, thinking
