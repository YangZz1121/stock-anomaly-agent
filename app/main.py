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

from app.chat import answer_chitchat, gate_intent, run_chat
from app.config import get_settings
from app.contracts import ResearchWindow, WINDOW_LABELS
from app.engine import guardrails
from app.engine.intent import (
    CHITCHAT_HINT,
    MODEL_REPLY,
    ConversationContext,
    IntentKind,
    estimate_report_minutes,
    notice_text,
)
from app.errors import NeedsWindowChoice, ResearchError
from app.providers.registry import build_providers, describe_providers
from app.trace import PROGRESS_STEPS, SNAPSHOT_STEPS, RunRecorder

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

app = FastAPI(
    title="个股异动研究 Agent",
    description="从价格变化出发，通过证据驱动研究，输出可验证的个股事件研究 Brief。",
    version="1.0.0",
)


class ResearchPayload(BaseModel):
    query: str
    window: Optional[ResearchWindow] = None
    context_stocks: Optional[List[str]] = None
    context_queries: Optional[List[str]] = None
    context_window: Optional[ResearchWindow] = None
    faults: Optional[List[str]] = None


@app.get("/api/health")
async def health() -> Dict[str, Any]:
    return {"status": "ok"}


@app.get("/api/config")
async def config() -> Dict[str, Any]:
    """前端启动时拉取运行状态，用于展示降级横幅。"""
    settings = get_settings()
    status = describe_providers(settings)
    return {
        "providers": status.labels,
        "degraded": status.degraded,
        "notices": guardrails.degraded_notice(status.labels, ""),
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
    context = _conversation_context(
        payload.context_stocks, payload.context_queries, payload.context_window
    )
    intent = _classify_or_http_error(payload.query, payload.window, context)
    if intent.kind == IntentKind.CHITCHAT and intent.chitchat_topic == "model":
        return JSONResponse(
            content={
                "kind": "chitchat",
                "message": MODEL_REPLY,
                "hint": intent.hint or CHITCHAT_HINT,
            }
        )
    settings = get_settings()
    providers = build_providers(settings, faults=set(payload.faults or []))
    recorder = RunRecorder(_run_id())
    try:
        result = await run_chat(
            payload.query, payload.window, providers, settings, recorder, context
        )
    except NeedsWindowChoice as exc:
        raise HTTPException(status_code=422, detail=_error_body(exc)) from exc
    except ResearchError as exc:
        raise HTTPException(status_code=422, detail=_error_body(exc)) from exc
    finally:
        await providers.aclose()
    if result.kind == "chitchat":
        return JSONResponse(
            content={
                "kind": "chitchat",
                "message": result.reply,
                "hint": result.hint,
            }
        )
    if len(result.briefs) == 1:
        return JSONResponse(content=json.loads(result.briefs[0].model_dump_json()))
    return JSONResponse(
        content={
            "kind": "bundle",
            "briefs": [json.loads(item.model_dump_json()) for item in result.briefs],
            "tasks": result.tasks,
        }
    )


@app.get("/api/research/stream")
async def research_stream(
    query: str = Query(..., min_length=1),
    window: Optional[ResearchWindow] = Query(None),
    context_stocks: Optional[str] = Query(None),
    context_queries: Optional[str] = Query(None),
    context_window: Optional[ResearchWindow] = Query(None),
    faults: Optional[str] = Query(None),
) -> StreamingResponse:
    fault_set = {f.strip() for f in (faults or "").split(",") if f.strip()}
    context = _conversation_context(
        _split_context(context_stocks),
        _split_context(context_queries, sep="\n"),
        context_window,
    )
    return StreamingResponse(
        _stream(query, window, fault_set, context),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # 禁用反向代理缓冲，否则进度条不会实时更新
        },
    )


async def _stream(
    query: str,
    window: Optional[ResearchWindow],
    faults: set,
    context: Optional[ConversationContext] = None,
):
    try:
        intent = gate_intent(query, window, context=context)
    except (NeedsWindowChoice, ResearchError) as exc:
        yield _sse("error", _error_body(exc))
        yield _sse("done", {})
        return

    if intent.kind == IntentKind.CHITCHAT:
        async for chunk in _stream_chitchat(query, intent, faults):
            yield chunk
        return

    settings = get_settings()
    providers = build_providers(settings, faults=faults)
    queue: asyncio.Queue = asyncio.Queue()
    recorder = RunRecorder(_run_id(), queue)
    task = None

    if intent.wants_full_report:
        minutes = estimate_report_minutes(
            window or intent.parsed.window, len(intent.parsed.search_keys)
        )
        yield _sse(
            "notice",
            {"message": notice_text(minutes), "minutes": minutes},
        )

    steps = SNAPSHOT_STEPS if intent.kind == IntentKind.SNAPSHOT else PROGRESS_STEPS
    yield _sse(
        "start",
        {
            "run_id": recorder.run_id,
            "steps": steps,
            "kind": intent.kind.value,
            "tasks": [{"key": key, "label": key} for key in intent.parsed.search_keys],
        },
    )

    task = asyncio.create_task(
        run_chat(query, window, providers, settings, recorder, context)
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
                while not queue.empty():
                    event = queue.get_nowait()
                    yield _sse(event["event"], event["data"])
                break
            yield _sse("heartbeat", {"elapsed_ms": recorder.elapsed_ms()})

        result = task.result()
        for brief in result.briefs:
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
        if task is not None and not task.done():
            task.cancel()
        await providers.aclose()
        yield _sse("done", {})


async def _stream_chitchat(query: str, intent, faults: set):
    if intent.chitchat_topic == "model":
        yield _sse(
            "reply",
            {
                "kind": "chitchat",
                "message": MODEL_REPLY,
                "hint": intent.hint or CHITCHAT_HINT,
            },
        )
        yield _sse("done", {})
        return

    settings = get_settings()
    providers = build_providers(settings, faults=faults)
    recorder = RunRecorder(_run_id())
    try:
        text = await answer_chitchat(query, intent, providers.llm, recorder)
        yield _sse(
            "reply",
            {
                "kind": "chitchat",
                "message": text,
                "hint": intent.hint or CHITCHAT_HINT,
            },
        )
    except Exception as exc:  # pragma: no cover
        yield _sse(
            "error",
            {
                "code": "internal_error",
                "message": "闲聊回复失败。",
                "hint": str(exc)[:300],
            },
        )
    finally:
        await providers.aclose()
        yield _sse("done", {})


def _conversation_context(
    stocks: Optional[List[str]],
    queries: Optional[List[str]],
    window: Optional[ResearchWindow],
) -> ConversationContext:
    return ConversationContext(
        stocks=[item.strip() for item in (stocks or []) if item and item.strip()],
        queries=[item.strip() for item in (queries or []) if item and item.strip()],
        window=window,
    )


def _split_context(raw: Optional[str], sep: str = ",") -> List[str]:
    if not raw:
        return []
    return [part.strip() for part in raw.split(sep) if part.strip()]


def _classify_or_http_error(
    query: str,
    window: Optional[ResearchWindow],
    context: Optional[ConversationContext] = None,
):
    try:
        return gate_intent(query, window, context=context)
    except NeedsWindowChoice as exc:
        raise HTTPException(status_code=422, detail=_error_body(exc)) from exc
    except ResearchError as exc:
        raise HTTPException(status_code=422, detail=_error_body(exc)) from exc


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
