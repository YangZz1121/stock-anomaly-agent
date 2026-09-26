"""候选驱动因素的四类验证（规划文档第 10 章）。

时间吻合、横截面吻合、特异性三项本质上是日期与数值的比较，因此完全放在
规则层——让模型去判断"-0.6% 和 -7.2% 是否同向"只会引入不必要的幻觉。
第四项机制合理性需要理解业务，才交给推理层。

这里还守着一条硬规则：缺少分钟级数据时，不推断"几点的新闻导致几点的下跌"。
"""

from __future__ import annotations

import re
from typing import List, Optional

from pydantic import BaseModel

from app.agent.evidence_collect import ClusterInfo, EvidenceWindow
from app.contracts import (
    CHECK_RESULT_LABELS,
    CheckResult,
    DriverCategory,
    ResearchWindow,
)
from app.engine import presenter
from app.schemas import DriverCheck

# A 股收盘时间。晚于这个点发布的内容不能用来解释当天的价格变化。
_MARKET_CLOSE = "15:00"
_TIME_RE = re.compile(r"(\d{1,2}):(\d{2})")

CHECK_LABELS = {
    "timing": "时间吻合",
    "cross_section": "横截面吻合",
    "specificity": "特异性",
    "mechanism": "机制合理性",
}


class MarketContext(BaseModel):
    """验证所需的确定性输入，全部由规则层计算好后传入。"""

    window: ResearchWindow
    window_days: List[str]
    evidence_window: EvidenceWindow
    stock_pct: Optional[float] = None
    industry_pct: Optional[float] = None
    market_pct: Optional[float] = None
    stock_vs_industry: Optional[float] = None
    industry_vs_market: Optional[float] = None
    gap_pct: Optional[float] = None
    post_open_pct: Optional[float] = None
    has_minute_data: bool = False
    divergence_threshold: float = 0.02
    industry_move_threshold: float = 0.02
    market_move_threshold: float = 0.01
    industry_name: Optional[str] = None
    stock_name: str = ""


def check_timing(cluster: ClusterInfo, ctx: MarketContext) -> DriverCheck:
    ew = ctx.evidence_window
    published = cluster.earliest_published

    if not published:
        return _check(
            "timing",
            CheckResult.UNKNOWN,
            "该事件缺少可用的发布时间，无法判断它与价格变化的先后关系。",
        )

    day = published[:10]
    has_time = _TIME_RE.search(published) is not None
    after_close = _is_after_close(published) and day >= ew.core_end

    if day > ew.core_end or after_close:
        when = "晚于研究窗口" if day > ew.core_end else "发布于收盘之后"
        return _check(
            "timing",
            CheckResult.FAIL,
            f"事件发布时间为 {published}，{when}，"
            f"不能用来解释 {ew.core_end} 及之前已经发生的价格变化。",
        )

    # 数据源只给到日期时，当天发布的消息究竟在收盘前还是收盘后无从判断。
    # 默认它能解释当天的价格变化等于凭空补了一个事实，所以降级为部分成立。
    if not has_time and day >= ew.core_end:
        return _check(
            "timing",
            CheckResult.PARTIAL,
            f"事件发布日期为 {day}，与研究窗口末日同一天，"
            f"但数据源只提供到日期、没有具体时间，无法确认它发布于 "
            f"{_MARKET_CLOSE} 收盘之前还是之后，"
            f"因此不能确定它先于当日价格变化发生。",
        )

    if cluster.in_core_window:
        detail = (
            f"事件最早发布于 {published}，落在核心证据窗口 "
            f"{ew.core_start} ~ {ew.core_end} 内。"
        )
        detail += _intraday_note(ctx, day)
        return _check("timing", CheckResult.PASS, detail)

    if ew.contains_extended(day):
        return _check(
            "timing",
            CheckResult.PARTIAL,
            f"事件发布于 {published}，早于核心窗口但仍在扩展窗口 "
            f"{ew.extended_start} 起的范围内，可能属于提前发生并持续发酵的信息，"
            f"时间关联性弱于核心窗口内的事件。",
        )

    return _check(
        "timing",
        CheckResult.FAIL,
        f"事件发布于 {published}，落在证据窗口之外。",
    )


def _intraday_note(ctx: MarketContext, event_day: str) -> str:
    """今日窗口下，指出价格变化主要发生在开盘还是盘中。"""
    if ctx.window != ResearchWindow.TODAY:
        return ""
    if ctx.gap_pct is None or ctx.post_open_pct is None:
        return ""
    gap, post = abs(ctx.gap_pct), abs(ctx.post_open_pct)
    if gap + post <= 0:
        return ""
    if gap >= post * 1.5:
        note = (
            "当日变化主要发生在开盘缺口"
            f"（缺口 {presenter.pct(ctx.gap_pct)}，开盘后 {presenter.pct(ctx.post_open_pct)}），"
            "因此前一交易日收盘后至当日开盘前的信息优先级更高。"
        )
    elif post >= gap * 1.5:
        note = (
            "当日变化主要发生在交易时段"
            f"（缺口 {presenter.pct(ctx.gap_pct)}，开盘后 {presenter.pct(ctx.post_open_pct)}），"
            "当日盘中信息优先级更高。"
        )
    else:
        note = "当日开盘缺口与盘中变化幅度接近，不足以据此区分信息发生的时点。"

    if not ctx.has_minute_data:
        note += "（缺少分钟级数据，不推断具体某条信息对应某一时刻的价格变化）"
    return " " + note


def check_cross_section(
    cluster: ClusterInfo, category: DriverCategory, ctx: MarketContext
) -> DriverCheck:
    fmt = presenter.pct
    facts = (
        f"个股 {fmt(ctx.stock_pct)}、行业 {fmt(ctx.industry_pct)}、"
        f"市场 {fmt(ctx.market_pct)}。"
    )

    if category == DriverCategory.INDUSTRY:
        if ctx.industry_pct is None or ctx.stock_pct is None:
            return _check(
                "cross_section",
                CheckResult.UNKNOWN,
                "缺少行业或个股区间涨跌数据，无法验证行业与同行是否出现类似表现。",
            )
        same_dir = _same_sign(ctx.industry_pct, ctx.stock_pct)
        relative = (
            ctx.industry_vs_market
            if ctx.industry_vs_market is not None
            else ctx.industry_pct
        )
        industry_moved = abs(relative) >= ctx.industry_move_threshold
        if same_dir and industry_moved:
            return _check(
                "cross_section",
                CheckResult.PASS,
                facts + "行业与个股同向，且行业相对市场的残差达到门槛，"
                "行业层面的解释在横截面上成立。",
            )
        if same_dir:
            return _check(
                "cross_section",
                CheckResult.PARTIAL,
                facts + "行业与个股方向一致，但行业相对市场的额外变化有限，"
                "行业因素的解释力有限（行业可能只是跟随大盘）。",
            )
        return _check(
            "cross_section",
            CheckResult.FAIL,
            facts + "行业整体表现与个股不同向，行业原因的解释力显著下降。",
        )

    if category == DriverCategory.MARKET:
        if ctx.market_pct is None or ctx.stock_pct is None:
            return _check(
                "cross_section",
                CheckResult.UNKNOWN,
                "缺少市场或个股区间涨跌数据，无法验证市场层面的同步性。",
            )
        if not _same_sign(ctx.market_pct, ctx.stock_pct):
            return _check(
                "cross_section",
                CheckResult.FAIL,
                facts + "市场整体表现与个股不同向，市场原因难以解释本次变化。",
            )
        if abs(ctx.market_pct) >= ctx.market_move_threshold:
            return _check(
                "cross_section",
                CheckResult.PASS,
                facts + "市场宽基指数与个股同向，且市场自身幅度达到门槛，"
                "市场层面的解释在横截面上成立。",
            )
        return _check(
            "cross_section",
            CheckResult.PARTIAL,
            facts + "市场与个股同向，但市场自身变动未达到门槛，"
            "市场因素只能提供弱解释。",
        )

    if category == DriverCategory.COMPANY:
        if ctx.stock_vs_industry is None:
            return _check(
                "cross_section",
                CheckResult.UNKNOWN,
                "缺少个股相对行业的偏离数据，无法验证是否存在公司特有变化。",
            )
        if abs(ctx.stock_vs_industry) >= ctx.divergence_threshold:
            return _check(
                "cross_section",
                CheckResult.PASS,
                facts
                + f"个股相对行业偏离 {presenter.pct_points(ctx.stock_vs_industry)}，"
                "存在需要由公司特有因素解释的部分。",
            )
        return _check(
            "cross_section",
            CheckResult.PARTIAL,
            facts
            + f"个股相对行业仅偏离 {presenter.pct_points(ctx.stock_vs_industry)}，"
            "个股基本跟随行业变动，公司特有因素的必要性下降。",
        )

    # 交易 / 关注度
    return _check(
        "cross_section",
        CheckResult.PARTIAL,
        "交易与关注度类因素反映的是资金与情绪层面的同步现象，"
        "不构成独立的横截面验证依据，只能作为辅助证据。",
    )


def check_specificity(
    cluster: ClusterInfo, category: DriverCategory, ctx: MarketContext
) -> DriverCheck:
    if category in (DriverCategory.MARKET, DriverCategory.INDUSTRY):
        if ctx.stock_vs_industry is None:
            return _check(
                "specificity",
                CheckResult.UNKNOWN,
                "缺少个股相对行业的偏离数据，无法判断该因素能否解释个股的额外变化。",
            )
        extra = ctx.stock_vs_industry
        if abs(extra) >= ctx.divergence_threshold:
            return _check(
                "specificity",
                CheckResult.PARTIAL,
                f"该因素作用于{'市场' if category == DriverCategory.MARKET else '行业'}整体，"
                f"但个股相对行业仍有 {presenter.pct_points(extra)} 的额外变化，"
                "这部分无法由该因素解释。",
            )
        return _check(
            "specificity",
            CheckResult.PASS,
            f"个股相对行业仅偏离 {presenter.pct_points(extra)}，"
            "个股表现基本可由该层面因素覆盖。",
        )

    if category == DriverCategory.COMPANY:
        names = ctx.stock_name
        mentioned = names and (
            names in cluster.title or names in cluster.summary
        )
        if cluster.scope == "company" or mentioned:
            return _check(
                "specificity",
                CheckResult.PASS,
                "该事件直接指向目标公司，具备解释个股相对行业额外变化的特异性。",
            )
        return _check(
            "specificity",
            CheckResult.PARTIAL,
            "该事件未直接指向目标公司，作为公司特有因素的特异性不足。",
        )

    return _check(
        "specificity",
        CheckResult.PARTIAL,
        "交易与关注度数据描述的是价格变化的伴随现象，不具备独立的解释特异性。",
    )


def _check(key: str, result: CheckResult, reasoning: str) -> DriverCheck:
    return DriverCheck(
        key=key,
        label=CHECK_LABELS[key],
        result=result,
        result_label=CHECK_RESULT_LABELS[result.value],
        reasoning=reasoning,
    )


def make_mechanism_check(result: CheckResult, reasoning: str) -> DriverCheck:
    return _check("mechanism", result, reasoning)


def _same_sign(a: float, b: float) -> bool:
    return (a > 0 and b > 0) or (a < 0 and b < 0)


def _is_after_close(published: str) -> bool:
    m = _TIME_RE.search(published or "")
    if not m:
        return False
    hh, mm = int(m.group(1)), int(m.group(2))
    ch, cm = (int(x) for x in _MARKET_CLOSE.split(":"))
    return (hh, mm) > (ch, cm)
