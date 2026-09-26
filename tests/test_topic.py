"""多轮公司主题：最新值覆盖，合并语义才并行。"""

from app.engine.intent import ConversationContext
from app.engine.topic import apply_topic_policy, latest_topic_companies, wants_merge


def test_latest_query_beats_stale_context_stocks():
    keys = latest_topic_companies(
        ConversationContext(
            stocks=["宁德时代"],
            queries=["宁德时代今天怎么了", "贵州茅台为什么跌"],
        )
    )
    assert keys == ["贵州茅台"]


def test_replace_does_not_merge():
    assert wants_merge("改看茅台") is False
    keys, inherited = apply_topic_policy(
        ["贵州茅台"],
        "改看茅台今天怎么了",
        ConversationContext(stocks=["宁德时代"]),
    )
    assert keys == ["贵州茅台"]
    assert inherited is False


def test_compare_merges_latest_topic():
    assert wants_merge("和茅台对比一下") is True
    keys, inherited = apply_topic_policy(
        ["贵州茅台"],
        "和茅台对比一下",
        ConversationContext(stocks=["宁德时代"]),
    )
    assert keys == ["贵州茅台", "宁德时代"]
    assert inherited is True
