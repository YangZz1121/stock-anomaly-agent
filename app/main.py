"""FastAPI 应用入口。

提供两条研究接口：
* ``POST /api/research`` —— 一次性返回完整 Brief，便于脚本和测试调用；
* ``GET  /api/research/stream`` —— SSE 流式返回研究进度，前端据此渲染进度条。

故障演练（``faults`` 参数）保留在接口层，用于现场演示"接口失败时产品
如何显式暴露缺口"，而不是让评审只能看文档描述。
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.config import get_settings
from app.contracts import ResearchWindow, WINDOW_LABELS
from app.engine import guardrails
from app.orchestrator import (
    NeedsWindowChoice,
    ResearchError,
    ResearchRequest,
    run_research,
)
from app.providers.registry import build_providers
from app.trace import PROGRESS_STEPS, RunRecorder

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

app = FastAPI(
    title="个股异动研究 Agent",
    description="从价格变化出发，通过证据驱动研究，输出可验证的个股事件研究 Brief。",
    version="1.0.0",
)


class ResearchPayload(BaseModel):
    query: str
    window: Optional[ResearchWindow] = None
    faults: Optional[List[str]] = None


@app.get("/api/health")
async def health() -> Dict[str, Any]:
    return {"status": "ok"}


@app.get("/api/config")
async def config() -> Dict[str, Any]:
    """前端启动时拉取运行状态，用于展示降级横幅。"""
    settings = get_settings()
    providers = build_providers(settings)
    await providers.aclose()
    return {
        "providers": providers.labels,
        "degraded": providers.degraded,
        "notices": guardrails.degraded_notice(providers.labels, ""),
        "disclaimers": guardrails.DISCLAIMERS,
        "windows": [
            {"value": w.value, "label": WINDOW_LABELS[w.value]} for w in ResearchWindow
        ],
        "steps": PROGRESS_STEPS,
        "thresholds": {
            "single_day_concentration": settings.single_day_concentration_threshold,
            "direction_consistency": settings.direction_consistency_threshold,
            "path_efficiency": settings.path_efficiency_threshold,
            "evidence_extended_window_days": settings.evidence_extended_window_days,
        },
    }


@app.post("/api/research")
async def research(payload: ResearchPayload) -> JSONResponse:
    settings = get_settings()
    providers = build_providers(settings, faults=set(payload.faults or []))
    recorder = RunRecorder(_run_id())
    try:
        brief = await run_research(
            ResearchRequest(query=payload.query, window=payload.window),
            providers,
            settings,
            recorder,
        )
    except NeedsWindowChoice as exc:
        raise HTTPException(status_code=422, detail=_error_body(exc)) from exc
    except ResearchError as exc:
        raise HTTPException(status_code=422, detail=_error_body(exc)) from exc
    finally:
        await providers.aclose()
    return JSONResponse(content=json.loads(brief.model_dump_json()))


@app.get("/api/research/stream")
async def research_stream(
    query: str = Query(..., min_length=1),
    window: Optional[ResearchWindow] = Query(None),
    faults: Optional[str] = Query(None),
) -> StreamingResponse:
    fault_set = {f.strip() for f in (faults or "").split(",") if f.strip()}
    return StreamingResponse(
        _stream(query, window, fault_set),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # 禁用反向代理缓冲，否则进度条不会实时更新
        },
    )


async def _stream(query: str, window: Optional[ResearchWindow], faults: set):
    settings = get_settings()
    providers = build_providers(settings, faults=faults)
    queue: asyncio.Queue = asyncio.Queue()
    recorder = RunRecorder(_run_id(), queue)

    yield _sse("start", {"run_id": recorder.run_id, "steps": PROGRESS_STEPS})

    task = asyncio.create_task(
        run_research(
            ResearchRequest(query=query, window=window), providers, settings, recorder
        )
    )

    try:
        while True:
            drain = asyncio.create_task(queue.get())
            done, _ = await asyncio.wait(
                {drain, task}, return_when=asyncio.FIRST_COMPLETED, timeout=30
            )
            if drain in done:
                event = drain.result()
                yield _sse(event["event"], event["data"])
                continue

            drain.cancel()
            if task in done:
                # 研究已结束，把队列里剩下的进度事件排空后再发最终结果
                while not queue.empty():
                    event = queue.get_nowait()
                    yield _sse(event["event"], event["data"])
                break
            yield _sse("heartbeat", {"elapsed_ms": recorder.elapsed_ms()})

        brief = task.result()
        yield _sse("brief", json.loads(brief.model_dump_json()))
    except (NeedsWindowChoice, ResearchError) as exc:
        yield _sse("error", _error_body(exc))
    except Exception as exc:  # pragma: no cover - 兜底，避免连接悬挂
        yield _sse(
            "error",
            {
                "code": "internal_error",
                "message": "研究过程中发生未预期的错误。",
                "hint": str(exc)[:300],
            },
        )
    finally:
        if not task.done():
            task.cancel()
        await providers.aclose()
        yield _sse("done", {})


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _error_body(exc: ResearchError) -> Dict[str, str]:
    return {"code": exc.code, "message": exc.message, "hint": exc.hint}


def _run_id() -> str:
    return uuid.uuid4().hex[:12]


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
