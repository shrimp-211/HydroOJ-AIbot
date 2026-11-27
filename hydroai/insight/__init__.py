"""学情诊断模块：从提交记录中挑选最值得分析的提交，长期追踪薄弱点。"""
from hydroai.insight.analyzer import Analysis
from hydroai.insight.selector import Selection
from hydroai.insight.service import InsightService
from hydroai.insight.store import InsightStore

__all__ = ["Analysis", "InsightService", "InsightStore", "Selection"]
