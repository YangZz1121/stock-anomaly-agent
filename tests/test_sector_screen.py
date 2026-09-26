"""板块扫描：口语板块映射 + 成分股异动名单。"""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from app.config import Settings
from app.contracts import ResearchWindow
from app.engine.intent import IntentKind, classify_intent
from app.engine.sector import resolve_sector_phrase
from app.main import app
from app.pipeline.sector_screen import run_sector_screen
from app.providers.registry import build_providers
from app.trace import RunRecorder


def test_resolve_tech_and_baijiu_groups():
    settings = Settings(
        market_provider="mock", evidence_provider="mock", llm_provider="mock"
    )
    tech = resolve_sector_phrase("科技股最近有明显异动的股票么", settings)
    assert tech is not None
    assert tech.label == "科技股"
    assert "半导体" in tech.names
    assert "软件开发" in tech.names

    liquor = resolve_sector_phrase("白酒最近怎么了", settings)
    assert liquor is not None
    assert liquor.label == "白酒"
    assert liquor.names == ["白酒"]


def test_named_company_still_beats_sector_phrase():
    intent = classify_intent("宁德时代最近有明显异动么")
    assert intent.kind == IntentKind.NEED_WINDOW
    assert intent.parsed.search_keys == ["宁德时代"]
    assert intent.sector_label is None


def test_industry_name_with_window_is_sector_screen():
    intent = classify_intent("半导体", ResearchWindow.D5)
    assert intent.kind == IntentKind.SECTOR_SCREEN
    assert intent.sector_label == "半导体"


def test_run_baijiu_screen_uses_fixture_constituents():
    settings = Settings(
        market_provider="mock", evidence_provider="mock", llm_provider="mock"
    )
    providers = build_providers(settings)

    async def _run():
        try:
            return await run_sector_screen(
                "白酒股最近有明显异动的股票么",
                ResearchWindow.D5,
                providers,
                settings,
                RunRecorder("sector-baijiu"),
            )
        finally:
            await providers.aclose()

    result = asyncio.run(_run())
    assert result.kind == "sector_screen"
    assert result.sector_label == "白酒股"
    assert result.industries == ["白酒"]
    assert result.scanned_count >= 1
    names = {item.stock.name for item in result.movers}
    assert names & {"贵州茅台", "五粮液"}


def test_http_tech_screen_returns_list():
    client = TestClient(app)
    response = client.post(
        "/api/research",
        json={"query": "科技股最近有明显异动的股票么", "window": "d5"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["kind"] == "sector_screen"
    assert body["sector_label"] == "科技股"
    assert body["scanned_count"] >= 1
    assert any(item["stock"]["name"] == "宁德时代" for item in body["movers"])
