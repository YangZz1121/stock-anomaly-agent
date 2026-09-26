"""合规护栏。

产品边界（规划 28 章）：不提供确定性股价预测、收益承诺、买卖建议和目标价。

护栏放在输出的最后一道，对**所有**自由文本做一次扫描。这么做的前提认知是：
提示词约束会被模型偶尔违反，而用户看到的是最终页面，所以最终页面必须有
一层不依赖模型自觉的拦截。
"""

from __future__ import annotations

import re
from typing import List, Tuple

# 直接的买卖建议
_ADVICE_PATTERNS = [
    re.compile(r"(建议|可以|应该|值得|不妨)\s*(买入|买进|加仓|抄底|建仓|卖出|减仓|清仓|止损)"),
    re.compile(r"(买入|卖出|增持|减持|持有)\s*(评级|建议)"),
    re.compile(r"(强烈推荐|推荐买入|建议配置|逢低吸纳|落袋为安)"),
]

# 确定性的涨跌预测与收益承诺
_PREDICTION_PATTERNS = [
    re.compile(r"(必将|一定会|肯定会|必然)\s*(上涨|下跌|涨|跌|反弹|回升)"),
    # 「股价将在下周反弹至 250 元」这类句子里，主语和动词之间常常隔着
    # 时间状语，所以要允许中间夹几个字，但不能跨句。
    re.compile(
        r"(股价|股票|该股)[^。；！？\n]{0,12}?(将|会|即将)[^。；！？\n]{0,12}?"
        r"(上涨|下跌|涨到|跌到|反弹|回升|创新高|创新低)"
    ),
    re.compile(r"目标价\s*[:：]?\s*\d"),
    re.compile(r"(预计|预期)\s*(收益率|回报)\s*(可达|不低于|超过)\s*\d"),
    re.compile(r"(稳赚|保本|包赚|无风险收益)"),
]

REDACTION = "［该表述涉及投资建议或确定性预测，已被产品护栏移除］"

DISCLAIMERS = [
    "本产品输出的是事件研究结论，不构成投资建议，不提供买入 / 卖出建议或目标价。",
    "本产品不对未来股价做确定性预测；所有基本面判断均针对公司经营，不等同于股价走势。",
    "事实、推断与不确定信息已分开标注；证据不足时产品会明确说明无法判断，而不是给出结论。",
    "数据缺失、来源冲突或接口调用失败时，产品会显式展示缺口，不会静默生成看似正常的结论。",
]


def scan_text(text: str) -> List[str]:
    """返回文本中命中的违规类型。"""
    hits: List[str] = []
    for pattern in _ADVICE_PATTERNS:
        if pattern.search(text or ""):
            hits.append("investment_advice")
            break
    for pattern in _PREDICTION_PATTERNS:
        if pattern.search(text or ""):
            hits.append("price_prediction")
            break
    return hits


def sanitize(text: str) -> Tuple[str, List[str]]:
    """命中即整句替换。

    这里不做"部分遮蔽"，因为一条被删掉半截的买卖建议，读起来仍然像建议。
    """
    hits = scan_text(text)
    if not hits:
        return text, []
    return REDACTION, hits


def sanitize_list(items: List[str]) -> Tuple[List[str], List[str]]:
    out: List[str] = []
    all_hits: List[str] = []
    for item in items:
        cleaned, hits = sanitize(item)
        out.append(cleaned)
        all_hits.extend(hits)
    return out, all_hits


def degraded_notice(labels: dict, reasoner_disclaimer: str) -> List[str]:
    """把当前的降级状态翻译成用户能看懂的一句话。"""
    notices: List[str] = []
    if labels.get("market") == "构造数据集":
        notices.append(
            "行情数据来自内置构造数据集，不是真实市场行情。配置 FUYAO_API_KEY 后将自动切换到扶摇金融数据 API。"
        )
    if labels.get("evidence") == "构造证据集":
        notices.append(
            "资讯与公告来自内置构造证据集，不是真实公开信息。配置 IFIND_MCP_URL 后将自动切换到 iFinD MCP。"
        )
    if reasoner_disclaimer:
        notices.append(reasoner_disclaimer)
    return notices
