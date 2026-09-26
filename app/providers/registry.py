"""Provider 装配。

一次研究运行持有一组 Provider 实例（``ProviderBundle``），运行结束统一关闭。
``faults`` 只在测试和"故障演练"模式下使用，用来人为制造接口失败。
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Set

from pydantic import BaseModel, ConfigDict

from app.config import Settings, get_settings
from app.providers.evidence_ifind import IFindEvidenceProvider
from app.providers.evidence_mock import MockEvidenceProvider
from app.providers.fuyao import FuyaoProvider
from app.providers.llm import DisabledLLM, OpenAICompatibleLLM
from app.providers.mock_market import MockMarketProvider


class ProviderBundle(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    market: Any
    evidence: Any
    llm: Any
    labels: Dict[str, str]
    degraded: bool

    async def aclose(self) -> None:
        for p in (self.market, self.evidence, self.llm):
            close = getattr(p, "aclose", None)
            if close is not None:
                await close()


class ProviderStatus(BaseModel):
    labels: Dict[str, str]
    degraded: bool


def describe_providers(settings: Optional[Settings] = None) -> ProviderStatus:
    """只根据配置声明当前数据源，不创建 HTTP 客户端。"""
    settings = settings or get_settings()
    market_choice = settings.resolved_market_provider()
    evidence_choice = settings.resolved_evidence_provider()
    llm_choice = settings.resolved_llm_provider()
    labels = {
        "market": _label(market_choice, "扶摇金融数据 API", "构造数据集"),
        "evidence": _label(evidence_choice, "iFinD MCP", "构造证据集"),
        "llm": (
            f"{settings.llm_model}" if llm_choice == "openai" else "确定性启发式推理层"
        ),
    }
    degraded = "mock" in {market_choice, evidence_choice, llm_choice}
    return ProviderStatus(labels=labels, degraded=degraded)


def build_providers(
    settings: Optional[Settings] = None, faults: Optional[Set[str]] = None
) -> ProviderBundle:
    settings = settings or get_settings()
    faults = faults or set()
    status = describe_providers(settings)

    market_choice = settings.resolved_market_provider()
    if market_choice == "fuyao":
        market: Any = FuyaoProvider(settings)
    else:
        market = MockMarketProvider(settings, faults=faults)

    evidence_choice = settings.resolved_evidence_provider()
    if evidence_choice == "ifind":
        evidence: Any = IFindEvidenceProvider(settings)
    else:
        evidence = MockEvidenceProvider(settings, faults=faults)

    llm_choice = settings.resolved_llm_provider()
    if llm_choice == "openai":
        llm: Any = OpenAICompatibleLLM(settings)
    else:
        llm = DisabledLLM()

    return ProviderBundle(
        market=market,
        evidence=evidence,
        llm=llm,
        labels=status.labels,
        degraded=status.degraded,
    )


def _label(choice: str, real: str, mock: str) -> str:
    return real if choice != "mock" else mock
