"""测试公共装配。

所有端到端测试都跑在构造数据集上：不依赖网络、不依赖密钥，
因此 CI 和评审现场跑出来的结果完全一致。
"""

from __future__ import annotations

import asyncio
from typing import Optional, Sequence

import pytest

from app.config import Settings
from app.contracts import ResearchWindow
from app.orchestrator import ResearchRequest, run_research
from app.providers.registry import build_providers
from app.schemas import ResearchBrief
from app.trace import RunRecorder


@pytest.fixture
def settings() -> Settings:
    """强制走构造数据集，避免本机 .env 里恰好有密钥时测试行为漂移。"""
    return Settings(
        market_provider="mock",
        evidence_provider="mock",
        llm_provider="mock",
    )


def research(
    query: str,
    window: Optional[ResearchWindow] = None,
    *,
    settings: Optional[Settings] = None,
    faults: Optional[Sequence[str]] = None,
) -> ResearchBrief:
    """同步跑一次完整研究，测试里用它当作"用户提了一个问题"。"""
    cfg = settings or Settings(
        market_provider="mock", evidence_provider="mock", llm_provider="mock"
    )

    async def _run() -> ResearchBrief:
        providers = build_providers(cfg, faults=set(faults or ()))
        try:
            return await run_research(
                ResearchRequest(query=query, window=window),
                providers,
                cfg,
                RunRecorder("test"),
            )
        finally:
            await providers.aclose()

    return asyncio.run(_run())


def all_free_text(brief: ResearchBrief) -> list:
    """收集 Brief 里所有会出现在页面上的自由文本，供合规扫描使用。"""
    texts = [
        brief.what_happened.summary,
        brief.what_happened.pattern_reason,
        brief.why_happened.priority_reason,
        brief.what_it_means.overall.reason,
        brief.what_it_means.overall.display,
    ]
    texts.extend(brief.open_questions.questions)
    for driver in brief.why_happened.drivers:
        texts.extend([driver.name, driver.summary, driver.relevance])
        texts.extend(driver.unresolved)
        for check in driver.checks:
            texts.append(check.reasoning)
        a = driver.assessment
        if a is None:
            continue
        texts.extend(
            [
                a.exposure_basis,
                a.direction_reason,
                a.horizon_reason,
                a.strength_reason,
                a.display_headline,
                a.suppression_reason or "",
            ]
        )
        texts.extend(step.text for step in a.chain)
        texts.extend(a.offsetting_factors)
        texts.extend(a.amplifying_factors)
        texts.extend(a.key_unknowns)
    return [t for t in texts if t]
