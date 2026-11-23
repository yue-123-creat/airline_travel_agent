from __future__ import annotations

import asyncio
import inspect
import json
import re
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from app.config import Settings


class QwenError(Exception):
    pass


class QwenClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.model = settings.qwen_chat_model
        self.base_url = settings.qwen_base_url.rstrip("/")
        self.api_key = settings.dashscope_api_key

    @property
    def configured(self) -> bool:
        return bool(self.api_key) and self.api_key != "your_dashscope_api_key_here"

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    async def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.configured:
            raise QwenError("DASHSCOPE_API_KEY not configured")
        url = f"{self.base_url}/chat/completions"
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(url, headers=self._headers(), json=payload)
            if resp.status_code >= 400:
                raise QwenError(f"Qwen HTTP {resp.status_code}: {resp.text[:300]}")
            return resp.json()

    async def chat(
        self,
        *,
        system: str,
        user: str,
        temperature: float = 0.2,
        max_tokens: int = 512,
        json_mode: bool = False,
        model: str | None = None,
    ) -> tuple[str, int, int]:
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        payload: dict[str, Any] = {
            "model": (model or self.model).strip() or self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        data = await self._post(payload)
        content = data["choices"][0]["message"]["content"] or ""
        usage = data.get("usage") or {}
        return (
            content,
            int(usage.get("prompt_tokens") or 0),
            int(usage.get("completion_tokens") or 0),
        )

    async def chat_stream(
        self,
        *,
        system: str,
        user: str,
        temperature: float = 0.45,
        max_tokens: int = 700,
        on_token: Callable[[str], Awaitable[None] | None] | None = None,
        model: str | None = None,
    ) -> tuple[str, int, int]:
        """流式对话；on_token 收到增量文本（灵犀式边生成边出）。"""
        if not self.configured:
            raise QwenError("DASHSCOPE_API_KEY not configured")
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        payload: dict[str, Any] = {
            "model": (model or self.model).strip() or self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        url = f"{self.base_url}/chat/completions"
        parts: list[str] = []
        tokens_in = 0
        tokens_out = 0
        async with httpx.AsyncClient(timeout=60.0) as client:
            async with client.stream("POST", url, headers=self._headers(), json=payload) as resp:
                if resp.status_code >= 400:
                    body = (await resp.aread())[:300].decode("utf-8", errors="replace")
                    raise QwenError(f"Qwen HTTP {resp.status_code}: {body}")
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    if line.startswith(":"):
                        continue
                    if not line.startswith("data:"):
                        continue
                    data_str = line[5:].strip()
                    if data_str == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue
                    usage = chunk.get("usage") or {}
                    if usage:
                        tokens_in = int(usage.get("prompt_tokens") or tokens_in or 0)
                        tokens_out = int(usage.get("completion_tokens") or tokens_out or 0)
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    delta = (choices[0].get("delta") or {}).get("content") or ""
                    if not delta:
                        continue
                    parts.append(delta)
                    if on_token is not None:
                        maybe = on_token(delta)
                        if inspect.isawaitable(maybe):
                            await maybe
        text = "".join(parts).strip()
        if not tokens_out and text:
            tokens_out = max(1, len(text) // 2)
        return text, tokens_in, tokens_out

    async def chat_with_tools(
        self,
        *,
        system: str,
        user: str,
        tools: list[dict[str, Any]],
        tool_dispatcher: Callable[[str, dict[str, Any]], dict[str, Any]],
        temperature: float = 0.2,
        max_tokens: int = 800,
        max_rounds: int = 3,
        final_nudge: str | None = None,
    ) -> tuple[str, int, int, list[dict[str, Any]]]:
        """OpenAI-compatible tool loop. Returns content, tokens_in, tokens_out, tool_trace."""
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        tokens_in = 0
        tokens_out = 0
        tool_trace: list[dict[str, Any]] = []

        rounds = max(1, max_rounds)
        for i in range(rounds):
            payload: dict[str, Any] = {
                "model": self.model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "tools": tools,
                "tool_choice": "auto",
            }
            data = await self._post(payload)
            usage = data.get("usage") or {}
            tokens_in += int(usage.get("prompt_tokens") or 0)
            tokens_out += int(usage.get("completion_tokens") or 0)

            msg = data["choices"][0]["message"]
            tool_calls = msg.get("tool_calls") or []
            if not tool_calls:
                return (msg.get("content") or "").strip(), tokens_in, tokens_out, tool_trace

            messages.append(
                {
                    "role": "assistant",
                    "content": msg.get("content"),
                    "tool_calls": tool_calls,
                }
            )

            # 同轮多个 tool_call 并行执行（dispatcher 多为同步 I/O）
            prepared: list[tuple[Any, str, dict[str, Any]]] = []
            for call in tool_calls:
                fn = call.get("function") or {}
                name = str(fn.get("name") or "")
                raw_args = fn.get("arguments") or "{}"
                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
                except json.JSONDecodeError:
                    args = {}
                if not isinstance(args, dict):
                    args = {}
                prepared.append((call, name, args))

            async def _run_one(name: str, args: dict[str, Any]) -> dict[str, Any]:
                return await asyncio.to_thread(tool_dispatcher, name, args)

            results = await asyncio.gather(
                *[_run_one(name, args) for _, name, args in prepared],
                return_exceptions=True,
            )
            for (call, name, args), result in zip(prepared, results, strict=False):
                if isinstance(result, BaseException):
                    result = {"ok": False, "error": f"tool_error:{type(result).__name__}:{result}"}
                tool_trace.append({"name": name, "args": args, "result": result})
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.get("id") or name,
                        "content": json.dumps(result, ensure_ascii=False),
                    }
                )

            # 最后一轮若仍在调工具：再强制生成一轮纯文本，避免空行程
            if i == rounds - 1:
                break

        messages.append(
            {
                "role": "user",
                "content": (final_nudge or "").strip()
                or "工具结果已齐。请不要再调用工具，直接按系统要求输出最终正文。",
            }
        )
        final_payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        data = await self._post(final_payload)
        usage = data.get("usage") or {}
        tokens_in += int(usage.get("prompt_tokens") or 0)
        tokens_out += int(usage.get("completion_tokens") or 0)
        content = (data["choices"][0]["message"].get("content") or "").strip()
        return content, tokens_in, tokens_out, tool_trace


def parse_json_object(text: str) -> dict[str, Any]:
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", text)
        if not match:
            raise
        return json.loads(match.group(0))
