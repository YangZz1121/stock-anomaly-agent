"""生成演示用的构造数据集。

这里所有行情与资讯都是**人工构造**的，不是真实市场数据。之所以不直接
手写 JSON，是为了让数据由一组显式的「日收益序列」确定性地推导出来，
从而保证：

* 演示场景的价格形态（单日冲击 / 持续 / 反转）是可控且可复现的；
* 规则层的计算结果可以被人工核对；
* 任何人都能看懂这批数据是怎么造出来的，不会误以为是真实行情。

运行：``python scripts/build_fixtures.py``
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date, timedelta
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(ROOT, "fixtures")

# 除周末外不排除法定节假日：构造数据集刻意保持简单，
# 真实交易日历由扶摇 /api/a-share/calendar/trading-days 提供。
TRADING_DAY_COUNT = 45


def build_calendar(anchor: date) -> List[str]:
    """生成截至 anchor（含）的连续工作日序列。"""
    days: List[str] = []
    cursor = anchor
    while len(days) < TRADING_DAY_COUNT:
        if cursor.weekday() < 5:
            days.append(cursor.strftime("%Y-%m-%d"))
        cursor -= timedelta(days=1)
    return sorted(days)


def build_bars(
    days: List[str],
    start_price: float,
    returns: Dict[str, Dict[str, float]],
    baseline_return: float = 0.0015,
    base_amount: float = 2.4e9,
) -> List[Dict[str, Any]]:
    """由日收益序列推导 OHLC。

    ``returns`` 以日期为键，可为每天指定：
      pct   —— 当日涨跌幅（收盘/前收 - 1）
      gap   —— 开盘缺口（开盘/前收 - 1）
      hi    —— 最高价相对 max(开,收) 的上探比例
      lo    —— 最低价相对 min(开,收) 的下探比例
      vol   —— 成交额相对基准的倍数
    未指定的日期使用 ``baseline_return`` 作为温和波动。
    """
    bars: List[Dict[str, Any]] = []
    prev_close = start_price
    for i, day in enumerate(days):
        spec = returns.get(day, {})
        pct = spec.get("pct", baseline_return * (1 if i % 2 == 0 else -1))
        gap = spec.get("gap", pct * 0.35)
        hi = spec.get("hi", 0.004)
        lo = spec.get("lo", 0.004)
        vol_mult = spec.get("vol", 1.0)

        open_price = prev_close * (1 + gap)
        close_price = prev_close * (1 + pct)
        high_price = max(open_price, close_price) * (1 + hi)
        low_price = min(open_price, close_price) * (1 - lo)
        amount = base_amount * vol_mult
        # 用成交额和均价倒推成交量，保证两者自洽
        avg_price = (high_price + low_price + close_price) / 3
        volume = amount / avg_price

        bars.append(
            {
                "date": day,
                "open": round(open_price, 3),
                "high": round(high_price, 3),
                "low": round(low_price, 3),
                "close": round(close_price, 3),
                "prev_close": round(prev_close, 3),
                "volume": round(volume, 0),
                "amount": round(amount, 2),
            }
        )
        prev_close = close_price
    return bars


def main() -> None:
    anchor = date.today()
    while anchor.weekday() >= 5:
        anchor -= timedelta(days=1)
    days = build_calendar(anchor)
    w = days[-5:]  # 最近 5 个交易日，即演示窗口
    d1, d2, d3, d4, d5 = w

    os.makedirs(os.path.join(FIXTURES, "market", "bars"), exist_ok=True)
    os.makedirs(os.path.join(FIXTURES, "evidence"), exist_ok=True)

    # ---------------- 交易日历 ----------------
    _dump("market/calendar.json", {"trading_days": days})

    # ---------------- 标的表 ----------------
    tickers = [
        {"thscode": "300750.SZ", "name": "宁德时代", "ticker": "300750", "exchange": "SZSE"},
        {"thscode": "600519.SH", "name": "贵州茅台", "ticker": "600519", "exchange": "SSE"},
        {"thscode": "600887.SH", "name": "伊利股份", "ticker": "600887", "exchange": "SSE"},
        {"thscode": "000858.SZ", "name": "五粮液", "ticker": "000858", "exchange": "SZSE"},
        {"thscode": "002594.SZ", "name": "比亚迪", "ticker": "002594", "exchange": "SZSE"},
    ]
    _dump("market/tickers.json", {"item": tickers})

    # ---------------- 指数体系 ----------------
    _dump(
        "market/indexes.json",
        {
            "market_index": {"thscode": "000300.SH", "name": "沪深300"},
            "industry_indexes": [
                {"thscode": "886041.TI", "name": "电池"},
                {"thscode": "886042.TI", "name": "白酒"},
                {"thscode": "886055.TI", "name": "乳制品"},
                {"thscode": "886060.TI", "name": "汽车整车"},
            ],
            "constituents": {
                "886041.TI": ["300750.SZ"],
                "886042.TI": ["600519.SH", "000858.SZ"],
                "886055.TI": ["600887.SH"],
                "886060.TI": ["002594.SZ"],
            },
        },
    )

    # ---------------- 场景 A：宁德时代，行业 + 公司共振持续下跌 ----------------
    # 5 日累计约 -9.6%，跌幅分散在多日（持续变化型），第 4 日最大
    _bars(
        "300750.SZ",
        days,
        220.0,
        {
            d1: {"pct": -0.021, "gap": -0.008, "vol": 1.3},
            d2: {"pct": -0.018, "gap": -0.006, "vol": 1.4},
            d3: {"pct": -0.012, "gap": -0.004, "vol": 1.2},
            d4: {"pct": -0.034, "gap": -0.019, "hi": 0.002, "lo": 0.009, "vol": 2.4},
            d5: {"pct": -0.015, "gap": -0.007, "vol": 1.6},
        },
    )
    # 电池行业指数：同步走弱但幅度小于个股
    _bars(
        "886041.TI",
        days,
        3100.0,
        {
            d1: {"pct": -0.016},
            d2: {"pct": -0.014},
            d3: {"pct": -0.009},
            d4: {"pct": -0.026},
            d5: {"pct": -0.011},
        },
        base_amount=9.0e10,
    )

    # ---------------- 场景 B：贵州茅台，市场同步下跌 ----------------
    _bars(
        "600519.SH",
        days,
        1480.0,
        {
            d4: {"pct": -0.004},
            d5: {"pct": -0.019, "gap": -0.012, "vol": 1.25},
        },
    )
    _bars(
        "886042.TI",
        days,
        4200.0,
        {d4: {"pct": -0.005}, d5: {"pct": -0.018}},
        base_amount=4.0e10,
    )

    # ---------------- 场景 C：伊利股份，个股独立大跌 ----------------
    _bars(
        "600887.SH",
        days,
        28.5,
        {
            d4: {"pct": -0.004},
            d5: {"pct": -0.081, "gap": -0.055, "hi": 0.001, "lo": 0.012, "vol": 3.6},
        },
    )
    _bars(
        "886055.TI",
        days,
        1850.0,
        {d4: {"pct": -0.002}, d5: {"pct": -0.006}},
        base_amount=6.0e9,
    )

    # ---------------- 大盘指数：整体温和 ----------------
    _bars(
        "000001.SH",
        days,
        3280.0,
        {
            d1: {"pct": -0.003},
            d2: {"pct": 0.002},
            d3: {"pct": -0.001},
            d4: {"pct": -0.005},
            d5: {"pct": -0.017, "gap": -0.009},
        },
        base_amount=5.2e11,
    )
    _bars(
        "000300.SH",
        days,
        3280.0,
        {
            d1: {"pct": -0.003},
            d2: {"pct": 0.002},
            d3: {"pct": -0.001},
            d4: {"pct": -0.005},
            d5: {"pct": -0.017, "gap": -0.009},
        },
        base_amount=5.2e11,
    )

    # 未在场景中出现但可被搜索到的标的，用于演示"有标的无数据"
    _bars("000858.SZ", days, 138.0, {d5: {"pct": -0.016}})
    _bars("002594.SZ", days, 265.0, {d5: {"pct": -0.009}})
    _bars(
        "886060.TI",
        days,
        2400.0,
        {d5: {"pct": -0.007}},
        base_amount=2.0e10,
    )

    # ---------------- 异动原因线索池 ----------------
    _dump(
        "market/anomaly_reasons.json",
        {
            "300750.SZ": {
                "stock_name": "宁德时代",
                "tag_name": "大跌",
                "analysis_content": "受海外市场准入政策调整预期影响，动力电池板块整体走弱。",
                "keyword_list": ["动力电池", "出口", "政策"],
            },
            "600887.SH": {
                "stock_name": "伊利股份",
                "tag_name": "大跌",
                "analysis_content": "公司披露业绩预告修正，市场对全年盈利预期下调。",
                "keyword_list": ["业绩预告", "乳制品"],
            },
        },
    )

    # ---------------- 证据库 ----------------
    _dump("evidence/events.json", {"events": _events(d1, d2, d3, d4, d5)})

    print(f"fixtures 已生成，窗口日期：{d1} ~ {d5}（共 {len(days)} 个构造交易日）")


def _events(d1: str, d2: str, d3: str, d4: str, d5: str) -> List[Dict[str, Any]]:
    """构造资讯事件。

    刻意包含三类"陷阱"，用于验证证据系统是否真的在工作：
    1. 同一事件的多家转载（考察事件聚类与独立来源判定）；
    2. 发布时间晚于价格变化的新闻（考察时间吻合验证）；
    3. 四级来源的传闻（考察"不能独立支撑结论"约束）。
    """
    return [
        # --- 场景 A：行业政策，一份原文 + 两家转载 ---
        {
            "event_id": "EVT-A1",
            "title": "某出口目的地市场发布动力电池准入新规征求意见稿",
            "summary": "征求意见稿拟提高进口动力电池的本地化含量与碳足迹披露要求，设置 18 个月过渡期，期满后不达标产品将无法获得市场准入。",
            "published_at": f"{d3} 19:40",
            "source_name": "主管部门官方网站",
            "source_tier_hint": "t1_authoritative",
            "origin_key": "ORIGIN-POLICY-A1",
            "cluster_hint": "CLS-BATTERY-POLICY",
            "scope": "industry",
            "keywords": ["动力电池", "准入", "碳足迹", "出口", "电池"],
            "related_names": ["电池", "动力电池", "新能源车"],
        },
        {
            "event_id": "EVT-A2",
            "title": "海外动力电池准入新规征求意见，行业出口面临新门槛",
            "summary": "财经媒体援引主管部门征求意见稿报道，新规对本地化含量提出明确要求，国内电池企业出口业务或受影响。",
            "published_at": f"{d3} 21:15",
            "source_name": "财联社",
            "source_tier_hint": "t2_professional",
            "origin_key": "ORIGIN-POLICY-A1",
            "cluster_hint": "CLS-BATTERY-POLICY",
            "scope": "industry",
            "keywords": ["动力电池", "准入", "出口", "电池"],
            "related_names": ["电池", "动力电池"],
        },
        {
            "event_id": "EVT-A3",
            "title": "动力电池出口新规解读：过渡期 18 个月",
            "summary": "转载并解读同一份征求意见稿，未提供新增独立信息。",
            "published_at": f"{d4} 08:05",
            "source_name": "证券时报",
            "source_tier_hint": "t2_professional",
            "origin_key": "ORIGIN-POLICY-A1",
            "cluster_hint": "CLS-BATTERY-POLICY",
            "scope": "industry",
            "keywords": ["动力电池", "出口", "电池"],
            "related_names": ["电池"],
        },
        {
            "event_id": "EVT-A4",
            "title": "公司年度报告：境外营业收入占比 32.6%",
            "summary": "年度报告显示，报告期内境外业务收入占营业总收入的 32.6%，主要来自动力电池产品出口。",
            "published_at": f"{d1}",
            "source_name": "公司年度报告",
            "source_tier_hint": "t1_authoritative",
            "origin_key": "ORIGIN-FILING-CATL",
            "cluster_hint": "CLS-CATL-EXPOSURE",
            "scope": "company",
            "keywords": ["境外收入", "出口", "年报", "占比"],
            "related_names": ["宁德时代"],
        },
        {
            "event_id": "EVT-A5",
            "title": "公司公告：与海外客户签署长期供货框架协议",
            "summary": "公司公告与一家海外整车厂商签署为期五年的供货框架协议，约定产能预留与价格调整机制。",
            "published_at": f"{d2}",
            "source_name": "公司公告",
            "source_tier_hint": "t1_authoritative",
            "origin_key": "ORIGIN-ANNOUNCE-CATL",
            "cluster_hint": "CLS-CATL-CONTRACT",
            "scope": "company",
            "keywords": ["长期协议", "供货", "海外客户", "框架协议"],
            "related_names": ["宁德时代"],
        },
        {
            "event_id": "EVT-A6",
            "title": "网传某电池厂商海外订单大幅削减",
            "summary": "社交平台流传的消息称某电池厂商海外订单被大幅削减，消息未注明原始出处，相关公司未作回应。",
            "published_at": f"{d4} 11:20",
            "source_name": "社交平台匿名帖",
            "source_tier_hint": "t4_unverified",
            "origin_key": "ORIGIN-RUMOR-1",
            "cluster_hint": "CLS-RUMOR-1",
            "scope": "company",
            "keywords": ["订单", "削减", "海外", "电池"],
            "related_names": ["宁德时代"],
        },
        # --- 场景 B：市场层面 ---
        {
            "event_id": "EVT-B1",
            "title": "隔夜外围主要股指普遍收跌",
            "summary": "隔夜海外主要股指普遍下跌，风险资产整体承压，市场风险偏好回落。",
            "published_at": f"{d5} 08:30",
            "source_name": "中国证券报",
            "source_tier_hint": "t2_professional",
            "origin_key": "ORIGIN-MACRO-1",
            "cluster_hint": "CLS-RISK-OFF",
            "scope": "market",
            "keywords": ["外围", "股指", "风险偏好", "市场"],
            "related_names": [],
        },
        {
            "event_id": "EVT-B2",
            "title": "消费板块资金面走弱，白酒子板块承压",
            "summary": "当日消费板块整体资金净流出，白酒等子板块跟随大盘调整。",
            "published_at": f"{d5} 15:30",
            "source_name": "上海证券报",
            "source_tier_hint": "t2_professional",
            "origin_key": "ORIGIN-SECTOR-1",
            "cluster_hint": "CLS-CONSUMER-WEAK",
            "scope": "industry",
            "keywords": ["白酒", "消费", "资金"],
            "related_names": ["白酒", "贵州茅台"],
        },
        # --- 场景 C：公司特有事件，含一条时间晚于价格变化的新闻 ---
        {
            "event_id": "EVT-C1",
            "title": "公司公告：下修全年业绩预告",
            "summary": "公司公告称，受原奶价格与渠道库存影响，将全年归母净利润预告区间下调，下调幅度为原区间中值的约 12%。",
            "published_at": f"{d4} 18:50",
            "source_name": "公司公告",
            "source_tier_hint": "t1_authoritative",
            "origin_key": "ORIGIN-ANNOUNCE-YILI",
            "cluster_hint": "CLS-YILI-GUIDANCE",
            "scope": "company",
            "keywords": ["业绩预告", "下修", "净利润", "原奶"],
            "related_names": ["伊利股份"],
        },
        {
            "event_id": "EVT-C2",
            "title": "乳制品板块午后走弱",
            "summary": "盘后复盘文章，梳理当日乳制品板块表现。该文发布于收盘之后，不能作为解释当日价格变化的原因。",
            "published_at": f"{d5} 16:40",
            "source_name": "某财经资讯站",
            "source_tier_hint": "t3_general",
            "origin_key": "ORIGIN-RECAP-1",
            "cluster_hint": "CLS-YILI-RECAP",
            "scope": "industry",
            "keywords": ["乳制品", "板块", "复盘"],
            "related_names": ["伊利股份", "乳制品"],
        },
        {
            "event_id": "EVT-C3",
            "title": "公司年度报告：原奶采购以长期协议为主",
            "summary": "年度报告披露，公司原奶采购中长期协议占比较高，并通过自有牧场覆盖部分需求。",
            "published_at": f"{d1}",
            "source_name": "公司年度报告",
            "source_tier_hint": "t1_authoritative",
            "origin_key": "ORIGIN-FILING-YILI",
            "cluster_hint": "CLS-YILI-EXPOSURE",
            "scope": "company",
            "keywords": ["原奶", "长期协议", "采购", "牧场"],
            "related_names": ["伊利股份"],
        },
    ]


def _bars(code: str, days, start: float, returns, base_amount: float = 2.4e9) -> None:
    bars = build_bars(days, start, returns, base_amount=base_amount)
    _dump(f"market/bars/{code}.json", {"thscode": code, "bars": bars})


def _dump(rel: str, payload: Dict[str, Any]) -> None:
    path = os.path.join(FIXTURES, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
