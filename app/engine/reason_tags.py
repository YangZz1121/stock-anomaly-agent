"""第三方异动标签规范化。

口径对齐 a-stock-data（simonlin1212/a-stock-data）里同花顺热点的
reason tags：编辑部/数据源给出的标签只作检索线索，不升格为结论。
本产品把扶摇 ``anomaly-analysis-stock`` 的 tag / 关键词按同一方式处理。
"""

from __future__ import annotations

from typing import Iterable, List

from app.providers.base import AnomalyReason


def normalize_reason_tags(reasons: Iterable[AnomalyReason]) -> List[str]:
    """保序去重：先标签名，再关键词，最后才用解读正文截断。"""
    clues: List[str] = []
    seen = set()
    for reason in reasons or []:
        candidates = []
        if reason.tag_name:
            candidates.append(reason.tag_name.strip())
        for word in reason.keywords or []:
            text = (word or "").strip()
            if text:
                candidates.append(text)
        if reason.content:
            candidates.append(reason.content.strip()[:40])
        for item in candidates:
            if item and item not in seen:
                seen.add(item)
                clues.append(item)
    return clues
