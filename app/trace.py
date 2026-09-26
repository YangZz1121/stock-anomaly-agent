"""研究轨迹记录与进度广播。

``RunRecorder`` 同时承担两件事：记录每次工具调用（供"查看研究过程"抽屉
回放），以及把阶段进度推送给 SSE 通道（供前端渲染 ✓ / → / ○ 进度条）。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional

from app.contracts import FetchStatus, ToolCall

# 前端进度条的固定步骤，保证顺序稳定
SNAPSHOT_STEPS: List[Dict[str, str]] = [
    {"key": "resolve", "label": "识别标的与研究窗口"},
    {"key": "quote", "label": "获取个股行情"},
    {"key": "profile", "label": "整理价格快照"},
]

PROGRESS_STEPS: List[Dict[str, str]] = [
    {"key": "resolve", "label": "识别标的、行业与研究窗口"},
    {"key": "quote", "label": "获取个股行情"},
    {"key": "market", "label": "对比市场"},
    {"key": "industry", "label": "对比行业"},
    {"key": "priority", "label": "确定研究优先级"},
    {"key": "retrieve", "label": "定向检索所属行业与公司资讯"},
    {"key": "drivers", "label": "生成并验证候选驱动因素"},
    {"key": "counter", "label": "寻找反向证据"},
    {"key": "exposure", "label": "确认公司暴露"},
    {"key": "transmission", "label": "分析基本面传导"},
    {"key": "verdict", "label": "形成研究结论"},
]


class RunRecorder:
    def __init__(self, run_id: str, queue: Optional["asyncio.Queue"] = None) -> None:
        self.run_id = run_id
        self.queue = queue
        self.started_at = time.perf_counter()
        self.tool_calls: List[ToolCall] = []
        self.llm_calls: List[Dict[str, Any]] = []
        self._seq = 0

    # ------------------------------------------------------------------
    # 工具调用
    # ------------------------------------------------------------------

    def record_tool(
        self,
        tool: str,
        params: Dict[str, Any],
        status: FetchStatus,
        provider: str,
        latency_ms: Optional[int] = None,
        note: Optional[str] = None,
        evidence_ids: Optional[List[str]] = None,
    ) -> ToolCall:
        self._seq += 1
        call = ToolCall(
            seq=self._seq,
            tool=tool,
            params=_scrub(params),
            status=status,
            provider=provider,
            latency_ms=latency_ms,
            note=note,
            evidence_ids=evidence_ids or [],
        )
        self.tool_calls.append(call)
        return call

    def record_llm(
        self,
        purpose: str,
        model: str,
        provider: str,
        latency_ms: int,
        ok: bool,
        note: Optional[str] = None,
        prompt_chars: int = 0,
        response_chars: int = 0,
    ) -> None:
        self.llm_calls.append(
            {
                "purpose": purpose,
                "model": model,
                "provider": provider,
                "latency_ms": latency_ms,
                "ok": ok,
                "note": note,
                "prompt_chars": prompt_chars,
                "response_chars": response_chars,
            }
        )

    @property
    def failed_tool_calls(self) -> int:
        return sum(1 for c in self.tool_calls if c.status != FetchStatus.OK)

    def elapsed_ms(self) -> int:
        return int((time.perf_counter() - self.started_at) * 1000)

    # ------------------------------------------------------------------
    # 进度广播
    # ------------------------------------------------------------------

    def emit(self, event: str, payload: Dict[str, Any]) -> None:
        if self.queue is None:
            return
        try:
            self.queue.put_nowait({"event": event, "data": payload})
        except asyncio.QueueFull:  # pragma: no cover - 队列设为无界
            pass

    def step(self, key: str, state: str, detail: Optional[str] = None) -> None:
        """state: running | done | failed | skipped"""
        self.emit(
            "step",
            {"key": key, "state": state, "detail": detail, "elapsed_ms": self.elapsed_ms()},
        )

    def log(self, message: str) -> None:
        self.emit("log", {"message": message, "elapsed_ms": self.elapsed_ms()})


_SECRET_HINTS = ("key", "token", "secret", "authorization", "password")


def _scrub(params: Dict[str, Any]) -> Dict[str, Any]:
    """轨迹会展示给用户，任何疑似密钥的字段一律脱敏。"""
    out: Dict[str, Any] = {}
    for k, v in (params or {}).items():
        if any(h in k.lower() for h in _SECRET_HINTS):
            out[k] = "***"
        else:
            out[k] = v
    return out
