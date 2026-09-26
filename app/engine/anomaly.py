"""异动闸门：先判断「这是不是异动」，再决定是否归因。

检测层复用两个开源口径：

* tick-stock-panel（shy3130/tick-stock-panel）的交易所偏离值
  —— 主板 3 日 ±20%、创业板/科创 ±30%、北交所 ±40%；
  10 日 +100%/−50%、30 日 +200%/−70%；以及放量。
* TradingAgents-astock 的涨跌停识别（见 ``ashare.py``）。
* 统计层用窗口前波动率把累计涨跌标准化，接近 Qlib 对异常收益的 z 分数用法。

闸门为假时，产品输出「未见显著异动」，不再检索资讯、不编驱动因素。
"""

from __future__ import annotations

import math
from typing import List, Optional, Sequence

from pydantic import BaseModel, Field

from app.engine.ashare import (
    LIMIT_LABELS,
    BoardRule,
    classify_board,
    detect_limit_state,
)
from app.engine.residual import ResidualBreakdown, daily_return
from app.providers.base import Bar


class AnomalyGate(BaseModel):
    is_anomaly: bool
    board: str
    board_label: str
    limit_pct: float
    reasons: List[str] = Field(default_factory=list)
    z_score: Optional[float] = None
    limit_state: Optional[str] = None
    exchange_hits: List[str] = Field(default_factory=list)
    volume_surge: bool = False
    note: str = ""


def _cumulative(bars: Sequence[Bar]) -> Optional[float]:
    usable = [b for b in bars if b.close is not None and b.prev_close not in (None, 0)]
    if not usable:
        return None
    return usable[-1].close / usable[0].prev_close - 1


def _lookback_vol(history: Sequence[Bar]) -> Optional[float]:
    rets = [item[1] for item in (daily_return(b) for b in history) if item]
    if len(rets) < 8:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    if var <= 1e-12:
        return None
    return math.sqrt(var)


def _last_n_move(bars: Sequence[Bar], n: int) -> Optional[float]:
    usable = [b for b in bars if b.close is not None and b.prev_close not in (None, 0)]
    if len(usable) < n:
        return None
    window = usable[-n:]
    return window[-1].close / window[0].prev_close - 1


def _exchange_hits(bars: Sequence[Bar], rule: BoardRule) -> List[str]:
    """tick-stock-panel：交易所 3 / 10 / 30 日偏离值。"""
    hits: List[str] = []
    d3 = _last_n_move(bars, 3)
    if d3 is not None and abs(d3) >= rule.d3_threshold:
        hits.append(f"3 日偏离 {d3:.1%} 触及{rule.label}口径 ±{rule.d3_threshold:.0%}")
    d10 = _last_n_move(bars, 10)
    if d10 is not None:
        if d10 >= rule.d10_up:
            hits.append(f"10 日上涨 {d10:.1%} 触及交易所 +{rule.d10_up:.0%} 口径")
        elif d10 <= -rule.d10_down:
            hits.append(f"10 日下跌 {d10:.1%} 触及交易所 −{rule.d10_down:.0%} 口径")
    d30 = _last_n_move(bars, 30)
    if d30 is not None:
        if d30 >= rule.d30_up:
            hits.append(f"30 日上涨 {d30:.1%} 触及交易所 +{rule.d30_up:.0%} 口径")
        elif d30 <= -rule.d30_down:
            hits.append(f"30 日下跌 {d30:.1%} 触及交易所 −{rule.d30_down:.0%} 口径")
    return hits


def evaluate_anomaly(
    *,
    thscode: str,
    name: str = "",
    window_bars: Sequence[Bar],
    history_bars: Sequence[Bar],
    stock_pct: Optional[float],
    market_pct: Optional[float],
    industry_vs_market: Optional[float],
    stock_vs_industry: Optional[float],
    turnover_ratio: Optional[float] = None,
    residual: Optional[ResidualBreakdown] = None,
    stock_abs_threshold: float = 0.02,
    market_move_threshold: float = 0.01,
    industry_move_threshold: float = 0.02,
    divergence_threshold: float = 0.02,
    z_threshold: float = 2.0,
    volume_surge_ratio: float = 2.0,
) -> AnomalyGate:
    rule = classify_board(thscode, name)
    window_days = max(1, len(window_bars))
    stock_threshold = stock_abs_threshold * (rule.limit_pct / 0.10) * math.sqrt(window_days)

    reasons: List[str] = []
    last = window_bars[-1] if window_bars else None
    limit_state = detect_limit_state(last, rule.limit_pct) if last else None
    if limit_state:
        reasons.append(f"当日{LIMIT_LABELS[limit_state]}（{rule.label}幅度 {rule.limit_pct:.0%}）")

    trail = list(history_bars) + list(window_bars)
    exchange_hits = _exchange_hits(trail, rule)
    reasons.extend(exchange_hits)

    volume_surge = turnover_ratio is not None and turnover_ratio >= volume_surge_ratio
    if volume_surge:
        reasons.append(f"成交额为近 20 日均值的 {turnover_ratio:.1f} 倍，属于放量")

    vol = _lookback_vol(history_bars)
    z_score = None
    if stock_pct is not None and vol is not None:
        window_vol = vol * math.sqrt(window_days)
        if window_vol > 0:
            z_score = stock_pct / window_vol
            if abs(z_score) >= z_threshold:
                reasons.append(
                    f"窗口涨跌相对历史波动的 |z|={abs(z_score):.1f} ≥ {z_threshold:.0f}"
                )

    if stock_pct is not None and abs(stock_pct) >= stock_threshold:
        reasons.append(
            f"个股窗口涨跌 {stock_pct:.2%} 达到{rule.label}门槛 {stock_threshold:.2%}"
        )
    if market_pct is not None and abs(market_pct) >= market_move_threshold:
        reasons.append(f"市场宽基窗口涨跌 {market_pct:.2%} 达到 {market_move_threshold:.0%} 门槛")
    if industry_vs_market is not None and abs(industry_vs_market) >= industry_move_threshold:
        reasons.append("行业相对市场的残差达到行业变动门槛")
    if stock_vs_industry is not None and abs(stock_vs_industry) >= divergence_threshold:
        reasons.append("个股相对行业的残差达到背离门槛")

    is_anomaly = bool(reasons)
    note = (
        "；".join(reasons) + "。"
        if is_anomaly
        else (
            f"{rule.label}个股窗口涨跌、相对残差、交易所偏离值、涨跌停与放量均未达到门槛，"
            "判定为未见显著异动，不展开归因。"
        )
    )
    if residual and residual.method == "ols_beta":
        note += " 相对比较使用窗口前 OLS β 残差。"
    return AnomalyGate(
        is_anomaly=is_anomaly,
        board=rule.board,
        board_label=rule.label,
        limit_pct=rule.limit_pct,
        reasons=reasons,
        z_score=z_score,
        limit_state=limit_state,
        exchange_hits=exchange_hits,
        volume_surge=volume_surge,
        note=note,
    )
