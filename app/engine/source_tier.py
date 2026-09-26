"""来源可信度判级与事件聚类。

判级是确定性的：同一个来源名永远得到同一个等级，不交给模型即兴判断。
聚类也放在规则层，因为"这几篇报道是不是同一件事"本质上是文本比较，
不需要、也不应该消耗模型的判断力。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Dict, List, Optional, Sequence, Set, Tuple

from app.contracts import EvidenceKind, SourceTier
from app.providers.base import RawEvent

# 一级：原始权威来源。能确认"这件事真的发生了"。
_T1_MARKERS = (
    "国务院", "发改委", "工信部", "财政部", "商务部", "央行", "人民银行",
    "证监会", "银保监", "金融监管总局", "交易所", "上交所", "深交所", "北交所",
    "主管部门", "部委", "监管机构", "国家统计局", "海关总署", "市场监管总局",
    "公司公告", "公司年度报告", "年度报告", "年报", "半年报", "季度报告",
    "招股说明书", "招股书", "正式公告", "官方网站", "官网公告",
)

# 二级：有明确采编责任体系的专业财经媒体
_T2_MARKERS = (
    "财联社", "中国证券报", "上海证券报", "证券时报", "证券日报",
    "新华社", "人民日报", "经济日报", "第一财经", "21世纪经济报道",
    "每日经济新闻", "界面新闻", "华尔街见闻", "路透", "彭博", "同花顺资讯事件库",
)

# 四级：无法确认来源，只能当检索线索
_T4_MARKERS = (
    "社交平台", "匿名", "网传", "传闻", "小道消息", "自媒体",
    "贴吧", "论坛", "股吧", "朋友圈", "未具名", "知情人士",
)

_KIND_BY_MARKER: Sequence[Tuple[Tuple[str, ...], EvidenceKind]] = (
    (("年度报告", "年报", "半年报", "季度报告", "招股说明书", "招股书"), EvidenceKind.FILING),
    (("公司公告", "正式公告"), EvidenceKind.ANNOUNCEMENT),
    (
        ("国务院", "发改委", "工信部", "财政部", "商务部", "央行", "人民银行",
         "证监会", "银保监", "金融监管总局", "主管部门", "部委", "监管机构"),
        EvidenceKind.POLICY,
    ),
)


@lru_cache(maxsize=512)
def classify_tier(source_name: str, hint: Optional[str] = None) -> SourceTier:
    """按来源名判级；``hint`` 来自数据源自带的等级提示，优先采用。"""
    if hint:
        try:
            return SourceTier(hint)
        except ValueError:
            pass
    name = source_name or ""
    if any(m in name for m in _T4_MARKERS):
        return SourceTier.T4_UNVERIFIED
    if any(m in name for m in _T1_MARKERS):
        return SourceTier.T1_AUTHORITATIVE
    if any(m in name for m in _T2_MARKERS):
        return SourceTier.T2_PROFESSIONAL
    return SourceTier.T3_GENERAL


def classify_kind(event: RawEvent) -> EvidenceKind:
    text = f"{event.source_name} {event.title}"
    for markers, kind in _KIND_BY_MARKER:
        if any(m in text for m in markers):
            return kind
    return EvidenceKind.NEWS


# --------------------------------------------------------------------------
# 事件聚类
# --------------------------------------------------------------------------


def cluster_events(
    events: List[RawEvent], similarity_threshold: float = 0.45
) -> Dict[str, List[RawEvent]]:
    """把描述同一件事的报道归到一个聚类。

    优先使用数据源给的 ``cluster_hint``；没有提示时退回标题 n-gram 相似度。
    """
    clusters: Dict[str, List[RawEvent]] = {}
    representatives: List[Tuple[str, Set[str]]] = []

    for event in events:
        if event.cluster_hint:
            clusters.setdefault(event.cluster_hint, []).append(event)
            continue

        tokens = _ngrams(event.title)
        matched: Optional[str] = None
        for cid, rep_tokens in representatives:
            if _jaccard(tokens, rep_tokens) >= similarity_threshold:
                matched = cid
                break
        if matched is None:
            matched = f"AUTO-{len(representatives) + 1}"
            representatives.append((matched, tokens))
        clusters.setdefault(matched, []).append(event)

    return clusters


def count_independent_sources(events: Sequence[RawEvent]) -> int:
    """同一聚类里有多少个真正独立的信源。

    转载同一份原文的 N 家媒体共享 ``origin_key``，只算 1 个。
    """
    keys = {e.origin_key or e.source_name for e in events}
    return len(keys)


def _ngrams(text: str, n: int = 3) -> Set[str]:
    cleaned = "".join(ch for ch in (text or "") if ch.strip())
    if len(cleaned) < n:
        return {cleaned} if cleaned else set()
    return {cleaned[i : i + n] for i in range(len(cleaned) - n + 1)}


def _jaccard(a: Set[str], b: Set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)
