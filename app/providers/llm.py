"""OpenAI 兼容协议的 LLM 客户端。

只做一件事：把 system + user 发出去，要回一个 JSON 对象。解析失败一律
视为调用失败，不做"尽力而为"的修补猜测——一个格式都保证不了的响应，
它的内容同样不该被当作证据链的一环。
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Dict, Optional

import httpx

from app.config import Settings
from app.contracts import FetchStatus, Fetched

_JSON_BLOCK = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


class OpenAICompatibleLLM:
    name = "openai"

    def __init__(self, settings: Settings) -> None:
        self.model = settings.llm_model
        self._max_retries = settings.llm_max_retries
        self._client = httpx.AsyncClient(
            base_url=settings.llm_base_url.rstrip("/"),
            timeout=settings.llm_timeout_s,
            headers={
                "Authorization": f"Bearer {settings.llm_api_key or ''}",
                "Content-Type": "application/json",
            },
        )
        self._has_key = bool(settings.llm_api_key)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def complete_json(
        self,
        purpose: str,
        system: str,
        user: str,
        schema_hint: Optional[Dict[str, Any]] = None,
    ) -> Fetched[Dict[str, Any]]:
        source = f"llm:{self.model}:{purpose}"
        if not self._has_key:
            return Fetched.failure(source, self.name, "未配置 LLM_API_KEY")

        if schema_hint:
            user = (
                f"{user}\n\n请严格按以下 JSON 结构返回，不要输出任何额外文字：\n"
                f"{json.dumps(schema_hint, ensure_ascii=False, indent=2)}"
            )

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
        }

        last_note = "未知错误"
        for attempt in range(self._max_retries + 1):
            started = time.perf_counter()
            try:
                resp = await self._client.post("/chat/completions", json=payload)
            except httpx.TimeoutException:
                last_note = "LLM 请求超时"
                continue
            except httpx.HTTPError as exc:
                last_note = f"LLM 网络错误：{exc}"
                continue

            if resp.status_code == 429:
                last_note = "LLM 触发频率限制"
                continue
            if resp.status_code >= 400:
                # response_format 不被支持时，去掉该字段重试一次
                if resp.status_code == 400 and "response_format" in payload:
                    payload.pop("response_format")
                    last_note = "目标模型不支持 response_format，已改用纯文本 JSON"
                    continue
                last_note = f"LLM HTTP {resp.status_code}：{resp.text[:200]}"
                continue

            try:
                body = resp.json()
                content = body["choices"][0]["message"]["content"]
            except (ValueError, KeyError, IndexError) as exc:
                last_note = f"LLM 响应结构异常：{exc}"
                continue

            parsed = _parse_json(content)
            if parsed is None:
                last_note = "LLM 返回内容无法解析为 JSON 对象"
                continue

            latency = int((time.perf_counter() - started) * 1000)
            return Fetched.success(
                parsed,
                source,
                self.name,
                note=f"attempt={attempt + 1}, latency_ms={latency}",
            )

        return Fetched.failure(source, self.name, last_note, FetchStatus.FAILED)


class DisabledLLM:
    """没有密钥时的占位实现，任何调用都显式失败。"""

    name = "mock"
    model = "none"

    async def aclose(self) -> None:
        return None

    async def complete_json(
        self,
        purpose: str,
        system: str,
        user: str,
        schema_hint: Optional[Dict[str, Any]] = None,
    ) -> Fetched[Dict[str, Any]]:
        return Fetched.failure(
            f"llm:disabled:{purpose}",
            self.name,
            "未配置 LLM，已改用确定性启发式推理层",
        )


def _parse_json(content: str) -> Optional[Dict[str, Any]]:
    content = (content or "").strip()
    if not content:
        return None
    block = _JSON_BLOCK.search(content)
    if block:
        content = block.group(1).strip()
    try:
        parsed = json.loads(content)
    except ValueError:
        start, end = content.find("{"), content.rfind("}")
        if start == -1 or end <= start:
            return None
        try:
            parsed = json.loads(content[start : end + 1])
        except ValueError:
            return None
    return parsed if isinstance(parsed, dict) else None
