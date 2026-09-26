"""数值到展示的统一转换。

只有这一处负责把 ``None`` 渲染成「数据不可用」。这样做的目的是杜绝
"0.0%" 这种把缺失伪装成正常值的写法出现在任何地方。
"""

from __future__ import annotations

from typing import Optional

from app.contracts import FetchStatus
from app.schemas import Measure

UNAVAILABLE = "数据不可用"

# 单位不只是给人看的后缀，它还决定了这个数值能不能套用涨跌语义。
# 只有 PCT 和 PCT_POINTS 带方向；占比、倍数、振幅都是无方向的幅度，
# 如果按红涨绿跌上色会被误读成「涨了 100%」。
UNIT_PCT = "pct"  # 涨跌幅，带方向
UNIT_PCT_POINTS = "pct_points"  # 相对差，单位百分点，带方向
UNIT_PCT_ABS = "pct_abs"  # 无方向的百分比幅度，如振幅
UNIT_SHARE = "share"  # 占比
UNIT_RATIO = "ratio"  # 相对倍数

DIRECTIONAL_UNITS = frozenset({UNIT_PCT, UNIT_PCT_POINTS})


def pct(value: Optional[float], digits: int = 2, signed: bool = True) -> str:
    if value is None:
        return UNAVAILABLE
    sign = "+" if (signed and value > 0) else ""
    return f"{sign}{value * 100:.{digits}f}%"


def pct_points(value: Optional[float], digits: int = 2) -> str:
    """相对差值用百分点表示，避免和涨跌幅混淆。"""
    if value is None:
        return UNAVAILABLE
    sign = "+" if value > 0 else ""
    return f"{sign}{value * 100:.{digits}f}pct"


def ratio(value: Optional[float], digits: int = 2) -> str:
    if value is None:
        return UNAVAILABLE
    return f"{value:.{digits}f}x"


def share(value: Optional[float]) -> str:
    if value is None:
        return UNAVAILABLE
    return f"{value * 100:.0f}%"


def amount(value: Optional[float]) -> str:
    if value is None:
        return UNAVAILABLE
    if abs(value) >= 1e8:
        return f"{value / 1e8:.2f} 亿元"
    if abs(value) >= 1e4:
        return f"{value / 1e4:.2f} 万元"
    return f"{value:.2f} 元"


def measure(
    key: str,
    label: str,
    value: Optional[float],
    display: str,
    *,
    unit: Optional[str] = None,
    caliber: Optional[str] = None,
    evidence_id: Optional[str] = None,
    note: Optional[str] = None,
) -> Measure:
    return Measure(
        key=key,
        label=label,
        value=value,
        display=display,
        unit=unit,
        caliber=caliber,
        evidence_id=evidence_id,
        note=note,
        status=FetchStatus.OK if value is not None else FetchStatus.MISSING,
    )
