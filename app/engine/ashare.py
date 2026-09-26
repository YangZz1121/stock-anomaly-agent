"""A 股板块与涨跌停口径。

规则移植自 TradingAgents-astock（simonlin1212/TradingAgents-astock）对
A 股交易制度的特化：主板 10%、创业板/科创板 20%、北交所 30%、ST 5%。
本模块只做确定性识别，不调用网络、不引入该项目的 LLM 分析师。
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel

from app.providers.base import Bar


class BoardRule(BaseModel):
    board: str
    label: str
    limit_pct: float
    # tick-stock-panel 交易所偏离值口径（相对前 N 日收盘）
    d3_threshold: float
    d10_up: float
    d10_down: float
    d30_up: float
    d30_down: float


_MAIN = BoardRule(
    board="main",
    label="主板",
    limit_pct=0.10,
    d3_threshold=0.20,
    d10_up=1.00,
    d10_down=0.50,
    d30_up=2.00,
    d30_down=0.70,
)
_CHINEXT = BoardRule(
    board="chinext",
    label="创业板",
    limit_pct=0.20,
    d3_threshold=0.30,
    d10_up=1.00,
    d10_down=0.50,
    d30_up=2.00,
    d30_down=0.70,
)
_STAR = BoardRule(
    board="star",
    label="科创板",
    limit_pct=0.20,
    d3_threshold=0.30,
    d10_up=1.00,
    d10_down=0.50,
    d30_up=2.00,
    d30_down=0.70,
)
_BSE = BoardRule(
    board="bse",
    label="北交所",
    limit_pct=0.30,
    d3_threshold=0.40,
    d10_up=1.00,
    d10_down=0.50,
    d30_up=2.00,
    d30_down=0.70,
)
_ST = BoardRule(
    board="st",
    label="ST / *ST",
    limit_pct=0.05,
    d3_threshold=0.20,
    d10_up=1.00,
    d10_down=0.50,
    d30_up=2.00,
    d30_down=0.70,
)


def classify_board(thscode: str, name: str = "") -> BoardRule:
    """按代码前缀与证券简称识别板块。"""
    text = name or ""
    if "ST" in text.upper() or "退市" in text:
        return _ST
    code = (thscode or "").split(".")[0]
    suffix = (thscode or "").split(".")[-1].upper() if "." in (thscode or "") else ""
    if suffix == "BJ" or code.startswith(("8", "4")):
        return _BSE
    if code.startswith(("688", "689")):
        return _STAR
    if code.startswith(("300", "301")):
        return _CHINEXT
    return _MAIN


def detect_limit_state(bar: Bar, limit_pct: float, tolerance: float = 0.002) -> Optional[str]:
    """判断当日是否触及涨跌停。容差 0.2 个百分点，覆盖四舍五入。"""
    if bar.prev_close in (None, 0) or bar.close is None:
        return None
    pct = bar.close / bar.prev_close - 1
    one_word = (
        bar.high is not None
        and bar.low is not None
        and bar.high == bar.low
        and bar.close == bar.high
    )
    if pct >= limit_pct - tolerance:
        return "limit_up_one_word" if one_word else "limit_up"
    if pct <= -limit_pct + tolerance:
        return "limit_down_one_word" if one_word else "limit_down"
    return None


LIMIT_LABELS = {
    "limit_up": "涨停",
    "limit_up_one_word": "一字涨停",
    "limit_down": "跌停",
    "limit_down_one_word": "一字跌停",
}
