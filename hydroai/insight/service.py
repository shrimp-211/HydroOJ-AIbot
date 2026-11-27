"""InsightService：把 OJ 数据、选择器、分析器、画像库串成可调用的服务。"""
from __future__ import annotations

import logging
import time

from hydroai.config.paths import data_dir
from hydroai.insight import profile as prof
from hydroai.insight.analyzer import Analysis, Analyzer
from hydroai.insight.selector import (AC, IGNORED, PENDING, Selection, code_hash,
                                      pick_for_problem, select_submissions)
from hydroai.insight.store import InsightStore, submit_time
from hydroai.oj.api import OJError
from hydroai.oj.resources import status_name

log = logging.getLogger(__name__)
MAX_RECORD_PAGES = 10


class InsightService:
    def __init__(self, api, config, store: InsightStore | None = None, ai=None, call_fn=None,
                 judge=None):
        self.api, self.config = api, config
        self.domain = api.domain
        self.store = store or InsightStore(data_dir() / config.get("insight_db", "insight.db"))
        self.analyzer = Analyzer(config, ai=ai, call_fn=call_fn, judge=judge)
        self._problems: dict[str, dict] = {}

    # ---- OJ 数据 ----
    def problem(self, pid) -> dict:
        key = str(pid)
        if key not in self._problems:
            try:
                self._problems[key] = self.api.problems.detail(pid)
            except OJError as exc:
                log.warning("[insight] 获取题目 %s 失败: %s", pid, exc)
                self._problems[key] = {"title": f"P{pid}", "content": "", "tags": []}
        return self._problems[key]

    def fetch_records(self, uid: int, *, pid=None, since_days: float | None = None) -> list[dict]:
        """拉取学生记录并缓存元数据。"""
        cutoff = time.time() - since_days * 86400 if since_days else 0
        out = []
        doc_id = self.problem(pid).get("docId") if pid is not None else None
        for r in self.api.records.iter_list(max_pages=MAX_RECORD_PAGES, uid=uid, pid=pid):
            if not self._matches(r, uid=uid, pid=doc_id if doc_id is not None else pid):
                continue
            if cutoff and submit_time(r) < cutoff:
                break  # 列表按时间倒序
            self.store.upsert_submission(self.domain, r)
            out.append(r)
        self.store.upsert_student(self.domain, uid, self.store.student_name(self.domain, uid)
                                  or self.api.users.name(uid))
        return out

    def _matches(self, rdoc, *, rid=None, uid=None, pid=None):
        if not isinstance(rdoc, dict) or not rdoc.get("_id") or rdoc.get("uid") is None or rdoc.get("pid") is None:
            return False
        if str(rdoc.get("domainId", "")) != str(self.domain):
            return False
        return ((rid is None or str(rdoc["_id"]) == str(rid)) and
                (uid is None or str(rdoc["uid"]) == str(uid)) and
                (pid is None or str(rdoc["pid"]) == str(pid)))

    def _full_record(self, rid: str, *, uid=None, pid=None) -> tuple[dict, dict]:
        data = self.api.records.get(rid)
        rdoc = data["rdoc"]
        if not self._matches(rdoc, rid=rid, uid=uid, pid=pid):
            raise OJError("提交记录的身份、题目或域与请求不符")
        if rdoc.get("code"):
            self.store.upsert_submission(self.domain, rdoc, rdoc["code"], code_hash(rdoc["code"]))
        return rdoc, data.get("pdoc") or {}

    def _reference_code(self, pid, uid: int) -> str:
        """只使用同一学生的 AC 代码，校验列表和详情。"""
        try:
            recs = self.api.records.list(pid=pid, uid=uid, status=AC).get("rdocs", [])
            for r in recs:
                if not self._matches(r, uid=uid, pid=pid) or r.get("status") != AC:
                    continue
                full, _ = self._full_record(r["_id"], uid=uid, pid=pid)
                if full.get("status") == AC and full.get("code"):
                    return full["code"]
        except OJError as exc:
            log.info("[insight] 无法获取学生自己的 AC 代码: %s", exc)
        return ""

    # ---- 选择 / 分析 ----
    def plan(self, uid: int, since_days: float | None = None, max_n: int | None = None) -> list[Selection]:
        """只做拉取与选择（不调用 AI），dry-run 与 scan 共用。"""
        max_n = max_n or int(self.config.get("insight_max_per_student", 5))
        since_days = since_days or self.config.get("insight_lookback_days", 30)
        rdocs = self.fetch_records(uid, since_days=since_days)
        analyzed = self.store.analyzed_rids(self.domain, uid)
        hashes = {s["rid"]: s["code_hash"] for s in self.store.submissions(self.domain, uid)
                  if s["code_hash"]}
        # 先粗选一批，取这些题的标签后再按「覆盖不同知识点」精选
        rough = select_submissions(rdocs, uid=uid, max_n=max_n * 3, analyzed=analyzed, hashes=hashes)
        tags = {s.pid: self.problem(s.pid).get("tags") for s in rough}
        return select_submissions(rdocs, uid=uid, max_n=max_n, analyzed=analyzed, hashes=hashes,
                                  tags_by_pid=tags)

    def analyze_record(self, rid: str, force: bool = False, use_ai: bool = True,
                       *, uid=None, pid=None) -> Analysis:
        cached = self.store.get_analysis(rid, self.domain)
        if cached and ((uid is not None and str(cached.get("uid")) != str(uid)) or
                       (pid is not None and str(cached.get("pid")) != str(pid))):
            raise OJError("缓存记录的学生或题目与请求不符")
        if not force and cached and (cached.get("ai_used") or cached.get("status") == AC or not use_ai):
            return Analysis.from_dict(cached)
        rdoc, _ = self._full_record(rid, uid=uid, pid=pid)
        problem = self.problem(rdoc.get("pid"))
        # 同一学生同题相同代码复用 AI 结果，保留当前评测的客观证据。
        duplicate = None
        current_hash = code_hash(rdoc.get("code", "")) if rdoc.get("code") else ""
        for sub in self.store.submissions(self.domain, int(rdoc["uid"]), rdoc["pid"]):
            old_hash = code_hash(sub["code"]) if sub.get("code") else sub["code_hash"]
            if sub["rid"] != rid and current_hash and old_hash == current_hash:
                previous = self.store.get_analysis(sub["rid"], self.domain)
                if previous and previous.get("ai_used"):
                    duplicate = previous
                    break
        ref = self._reference_code(rdoc.get("pid"), int(rdoc.get("uid", 0))) \
            if rdoc.get("status") != AC and not duplicate and use_ai else ""
        analysis = self.analyzer.analyze(rdoc, problem, reference=ref, use_ai=use_ai and not duplicate)
        if duplicate and analysis.status != AC:
            prior = Analysis.from_dict(duplicate)
            analysis.error_type = prior.error_type
            analysis.root_cause = prior.root_cause
            analysis.suggestions = prior.suggestions
            analysis.repair_items = prior.repair_items
            analysis.missing_concepts = prior.missing_concepts
            if prior.status == analysis.status and prior.score == analysis.score:
                analysis.distance_level = prior.distance_level
            analysis.ai_used = True
            analysis.evidence["reused_analysis_rid"] = duplicate["rid"]
        self.store.save_analysis(self.domain, analysis.to_dict(), self.analyzer.model)
        return analysis

    def scan_user(self, uid: int, since_days: float | None = None, max_n: int | None = None) -> list[Analysis]:
        results = []
        for sel in self.plan(uid, since_days, max_n):
            log.info("[insight] 分析 uid=%s P%s rid=%s：%s", uid, sel.pid, sel.rid, sel.reason)
            try:
                results.append(self.analyze_record(sel.rid, uid=uid, pid=sel.pid))
            except OJError as exc:
                log.warning("[insight] 分析 %s 失败: %s", sel.rid, exc)
        return results

    def active_users(self, since_days: float, max_users: int) -> list[int]:
        """从全域最近记录里发现活跃学生（按最近出现顺序）。"""
        cutoff, users = time.time() - since_days * 86400, []
        for r in self.api.records.iter_list(max_pages=5):
            if submit_time(r) < cutoff:
                break
            uid = int(r.get("uid", 0))
            if uid and uid != self.api.uid and uid not in users:
                users.append(uid)
            if len(users) >= max_users:
                break
        return users

    def scan_domain(self, max_users: int = 20, since_days: float | None = None,
                    max_n: int | None = None) -> dict[int, list[Analysis]]:
        since_days = since_days or self.config.get("insight_lookback_days", 30)
        return {uid: self.scan_user(uid, since_days, max_n)
                for uid in self.active_users(since_days, max_users)}

    # ---- 画像 / 报告 ----
    def profile(self, uid: int) -> dict:
        result = prof.build_profile(self.store.events(self.domain, uid))
        history = self.store.submissions(self.domain, uid)
        valid = [r for r in history if r["status"] not in PENDING | IGNORED]
        result["submission_history"] = {
            "total": len(valid), "accepted": sum(r["status"] == AC for r in valid),
            "failed": sum(r["status"] != AC for r in valid),
            "solved_pids": sorted({r["pid"] for r in valid if r["status"] == AC}),
        }
        return result

    def _name(self, uid: int) -> str:
        return self.store.student_name(self.domain, uid) or str(uid)

    def report(self, uid: int) -> str:
        analyses = self.store.analyses(self.domain, uid)
        clean = [{**Analysis.from_dict(a).to_dict(), "_at": a.get("_at", 0)} for a in analyses]
        return prof.student_report(self._name(uid), uid, self.profile(uid), clean)

    def class_report(self, uids: list[int] | None = None) -> str:
        uids = uids or self.store.students(self.domain)
        return prof.class_report(self.domain, {u: (self._name(u), self.profile(u)) for u in uids})

    # ---- 给 assistant 的契约 ----
    def student_context(self, uid: int, pid, *, analyze: bool = True) -> dict:
        """某学生在某题上的情况：最值得讲解的提交、尝试次数、是否已解、分析与画像摘要。"""
        ctx = {"uid": uid, "pid": pid, "best_submission": None, "attempts": 0, "solved": False,
               "analysis": None, "profile_summary": prof.summary_text(self.profile(uid))}
        try:
            recs = self.fetch_records(uid, pid=pid)
        except OJError as exc:
            log.warning("[insight] 获取学生记录失败: %s", exc)
            return ctx
        valid = [r for r in recs if int(r.get("status", 0) or 0) not in PENDING | IGNORED]
        ctx["attempts"] = len(valid)
        ctx["solved"] = any(r.get("status") == AC for r in valid)
        picked = pick_for_problem(valid)
        # 一次通过/无失败可选时：讲解最近一次 AC，否则最近一条
        best = picked[0] if picked else (
            max((r for r in valid if r.get("status") == AC), key=submit_time, default=None)
            or max(valid, key=submit_time, default=None))
        if not best:
            return ctx
        try:
            rdoc, _ = self._full_record(best["_id"], uid=uid, pid=best["pid"])
        except OJError as exc:
            log.warning("[insight] 获取提交代码失败: %s", exc)
            return ctx
        ctx["best_submission"] = {
            "rid": rdoc["_id"], "status": rdoc.get("status"), "status_name": status_name(rdoc.get("status")),
            "score": rdoc.get("score"), "lang": rdoc.get("lang"), "code": rdoc.get("code", ""),
            "submit_at": submit_time(rdoc)}
        analysis = self.store.get_analysis(rdoc["_id"], self.domain)
        if analysis and (str(analysis.get("uid")) != str(uid) or str(analysis.get("pid")) != str(best["pid"])):
            analysis = None
        if analyze and (analysis is None or (not analysis.get("ai_used") and analysis.get("status") != AC)):
            try:
                analysis = self.analyze_record(rdoc["_id"], uid=uid, pid=best["pid"]).to_dict()
            except Exception as exc:  # AI 不可用等：助手仍可凭代码本身作答
                log.warning("[insight] 分析失败，返回无分析的上下文: %s", type(exc).__name__)
        if analysis:
            analysis = Analysis.from_dict(analysis).to_dict()
        ctx["analysis"] = analysis
        ctx["profile_summary"] = prof.summary_text(self.profile(uid))
        return ctx
