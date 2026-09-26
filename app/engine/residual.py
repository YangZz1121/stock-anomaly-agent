"""窗口超额收益 / 残差。

算法对齐 microsoft/qlib 的常见做法：用窗口开始前的日收益做 OLS，
估计个股对市场、个股对行业的 beta，再用

    残差 ≈ 窗口累计涨跌 − β × 基准累计涨跌

解释本次窗口。beta 只使用窗口前的样本，避免把正在解释的这段收益
折进暴露估计。估不出 beta 时返回空，调用方退回原始相减。
"""

from __future__ import annotations

from typing import List, Optional, Sequence, Tuple

from pydantic import BaseModel

from app.providers.base import Bar


class ResidualBreakdown(BaseModel):
    beta_market: Optional[float] = None
    beta_industry: Optional[float] = None
    industry_beta_to_market: Optional[float] = None
    stock_vs_market: Optional[float] = None
    stock_vs_industry: Optional[float] = None
    industry_vs_market: Optional[float] = None
    sample_days: int = 0
    method: str = "raw_subtract"
    note: str = ""


def daily_return(bar: Bar) -> Optional[Tuple[str, float]]:
    if bar.close is None or bar.prev_close in (None, 0):
        return None
    return bar.date, bar.close / bar.prev_close - 1


def _series(bars: Sequence[Bar]) -> List[Tuple[str, float]]:
    out: List[Tuple[str, float]] = []
    for bar in bars:
        item = daily_return(bar)
        if item is not None:
            out.append(item)
    return out


def _align(
    left: Sequence[Tuple[str, float]], right: Sequence[Tuple[str, float]]
) -> Tuple[List[float], List[float]]:
    lookup = {d: v for d, v in right}
    xs: List[float] = []
    ys: List[float] = []
    for day, value in left:
        if day in lookup:
            ys.append(value)
            xs.append(lookup[day])
    return ys, xs


def ols_beta(
    y: Sequence[float],
    x: Sequence[float],
    *,
    min_obs: int = 10,
    clip_low: float = 0.2,
    clip_high: float = 2.5,
) -> Optional[float]:
    """单因子 OLS 斜率：cov(x, y) / var(x)。"""
    if len(y) < min_obs or len(y) != len(x):
        return None
    mean_x = sum(x) / len(x)
    mean_y = sum(y) / len(y)
    var_x = sum((xi - mean_x) ** 2 for xi in x)
    if var_x <= 1e-12:
        return None
    cov = sum((xi - mean_x) * (yi - mean_y) for xi, yi in zip(x, y))
    beta = cov / var_x
    return max(clip_low, min(clip_high, beta))


def estimate_residuals(
    *,
    stock_history: Sequence[Bar],
    market_history: Sequence[Bar],
    industry_history: Sequence[Bar],
    stock_cum: Optional[float],
    market_cum: Optional[float],
    industry_cum: Optional[float],
    min_obs: int = 10,
) -> ResidualBreakdown:
    stock_r = _series(stock_history)
    market_r = _series(market_history)
    industry_r = _series(industry_history)

    y_m, x_m = _align(stock_r, market_r)
    y_i, x_i = _align(stock_r, industry_r)
    y_im, x_im = _align(industry_r, market_r)

    beta_m = ols_beta(y_m, x_m, min_obs=min_obs)
    beta_i = ols_beta(y_i, x_i, min_obs=min_obs)
    beta_im = ols_beta(y_im, x_im, min_obs=min_obs)
    sample = max(len(y_m), len(y_i), len(y_im))

    stock_vs_market = (
        stock_cum - beta_m * market_cum
        if stock_cum is not None and market_cum is not None and beta_m is not None
        else None
    )
    stock_vs_industry = (
        stock_cum - beta_i * industry_cum
        if stock_cum is not None and industry_cum is not None and beta_i is not None
        else None
    )
    industry_vs_market = (
        industry_cum - beta_im * market_cum
        if industry_cum is not None and market_cum is not None and beta_im is not None
        else None
    )

    used = any(v is not None for v in (stock_vs_market, stock_vs_industry, industry_vs_market))
    if not used:
        return ResidualBreakdown(
            sample_days=sample,
            method="raw_subtract",
            note="窗口前重叠交易日不足，无法估计 beta，三层对比退回原始涨跌相减。",
        )

    parts = []
    if beta_m is not None:
        parts.append(f"个股对市场 β={beta_m:.2f}")
    if beta_i is not None:
        parts.append(f"个股对行业 β={beta_i:.2f}")
    if beta_im is not None:
        parts.append(f"行业对市场 β={beta_im:.2f}")
    return ResidualBreakdown(
        beta_market=beta_m,
        beta_industry=beta_i,
        industry_beta_to_market=beta_im,
        stock_vs_market=stock_vs_market,
        stock_vs_industry=stock_vs_industry,
        industry_vs_market=industry_vs_market,
        sample_days=sample,
        method="ols_beta",
        note="残差 = 窗口累计涨跌 − β × 基准累计涨跌；β 由窗口前重叠日收益 OLS 估计。"
        + ("（" + "，".join(parts) + "）" if parts else ""),
    )
