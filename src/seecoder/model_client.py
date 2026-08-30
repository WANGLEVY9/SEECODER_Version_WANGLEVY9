"""A narrow adapter around an OpenAI-compatible native tool-calling API."""

from __future__ import annotations

import re
import time
from typing import Any, Protocol

from seecoder.config import Settings
from seecoder.types import ChatMessage, ModelResponse, StreamEvent, ToolCall, Usage


_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_LATIN_RE = re.compile(r"[A-Za-z]")


def _language_instruction(messages: list[ChatMessage]) -> str:
    """Build a request-local language policy from the latest user turn.

    The instruction is deliberately added to every provider request so it also
    applies after retries, resumed sessions, and context compaction. Code,
    paths, identifiers, and quoted text remain unchanged.
    """

    latest = next((message.content or "" for message in reversed(messages) if message.role == "user"), "")
    cjk = len(_CJK_RE.findall(latest))
    latin = len(_LATIN_RE.findall(latest))
    if cjk and cjk / max(1, cjk + latin) >= 0.15:
        return (
            "语言约束：请使用简体中文回答最近一条用户消息。所有解释、状态、错误和总结都使用中文；\n"
            "代码、命令、文件路径、标识符和用户引用的原文保持原样。"
        )
    if latin:
        return (
            "Language policy: respond in English, matching the latest user message. Keep code, commands, "
            "file paths, identifiers, and quoted text unchanged."
        )
    return (
        "Language policy: respond in the same natural language as the latest user message. "
        "Keep code, commands, file paths, identifiers, and quoted text unchanged."
    )


class ModelClientError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        retryable: bool,
        partial_text: str | None = None,
        partial_reasoning: str | None = None,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.partial_text = partial_text
        self.partial_reasoning = partial_reasoning


class ModelClient(Protocol):
    def complete(self, messages: list[ChatMessage], tools: list[dict[str, Any]]) -> ModelResponse: ...

    def complete_stream(self, messages: list[ChatMessage], tools: list[dict[str, Any]]):
        """Yield streaming events and finish with a ``done`` event.

        Adapters may implement this natively or let ``RetryingModelClient``
        provide the non-streaming fallback. Keeping the method in the protocol
        makes the runner contract explicit and removes an unsafe type escape.
        """
        ...


def _as_openai_message(message: ChatMessage) -> dict[str, Any]:
    data: dict[str, Any] = {"role": message.role, "content": message.content}
    if message.tool_calls:
        data["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": call.arguments},
            }
            for call in message.tool_calls
        ]
    if message.tool_call_id:
        data["tool_call_id"] = message.tool_call_id
    if message.reasoning_content is not None:
        data["reasoning_content"] = message.reasoning_content
    return data


def _chat_completion_request(
    settings: Settings, messages: list[ChatMessage], tools: list[dict[str, Any]]
) -> dict[str, Any]:
    serialized_messages = [_as_openai_message(message) for message in messages]
    language_instruction = _language_instruction(messages)
    if serialized_messages and serialized_messages[0].get("role") == "system":
        serialized_messages[0]["content"] = (
            language_instruction + "\n\n" + (serialized_messages[0].get("content") or "")
        )
    else:
        serialized_messages.insert(0, {"role": "system", "content": language_instruction})
    request: dict[str, Any] = {
        "model": settings.model,
        "messages": serialized_messages,
        "tools": tools,
        "tool_choice": "auto",
        "temperature": 0,
    }
    # DeepSeek V4 defaults to thinking mode. P0 explicitly disables it in
    # the checked-in DeepSeek configuration until the optional P1 pathway
    # can preserve reasoning_content across every tool-call continuation.
    if settings.thinking_mode != "provider_default":
        request["extra_body"] = {"thinking": {"type": settings.thinking_mode}}
    if settings.thinking_mode == "enabled":
        request["reasoning_effort"] = settings.reasoning_effort
    return request


def _delta_reasoning(delta: Any) -> str | None:
    """Read a streaming reasoning piece across openai client versions."""

    direct = getattr(delta, "reasoning_content", None)
    if isinstance(direct, str):
        return direct
    extra = getattr(delta, "model_extra", None)
    if isinstance(extra, dict) and isinstance(extra.get("reasoning_content"), str):
        return extra["reasoning_content"]
    return None


def _usage_from_response(response: Any) -> Usage | None:
    """Read token usage from a provider response without making it mandatory."""

    usage = getattr(response, "usage", None)
    if usage is None:
        return None
    get = lambda name: getattr(usage, name, 0) or 0
    prompt = get("prompt_tokens")
    completion = get("completion_tokens")
    total = get("total_tokens") or (prompt + completion)
    if prompt == 0 and completion == 0 and total == 0:
        return None
    return Usage(prompt_tokens=prompt, completion_tokens=completion, total_tokens=total)


def _reasoning_content_from_provider_message(message: Any) -> str | None:
    """Read DeepSeek's extension field across OpenAI client versions.

    Some versions expose `reasoning_content` only in Pydantic's `model_extra`,
    while others promote it to a normal attribute. Accept either representation
    without logging the private reasoning text.
    """

    direct = getattr(message, "reasoning_content", None)
    if isinstance(direct, str):
        return direct
    extra = getattr(message, "model_extra", None)
    if isinstance(extra, dict) and isinstance(extra.get("reasoning_content"), str):
        return extra["reasoning_content"]
    dump_method = getattr(message, "model_dump", None)
    if callable(dump_method):
        dumped = dump_method()
        if isinstance(dumped, dict) and isinstance(dumped.get("reasoning_content"), str):
            return dumped["reasoning_content"]
    return None


class OpenAICompatibleClient:
    """One provider adapter; agent ownership remains outside the vendor client."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        try:
            from openai import OpenAI
        except ImportError as error:
            raise ModelClientError(
                "The 'openai' package is not installed. Run 'uv sync' before using a real model.",
                retryable=False,
            ) from error
        self._client = OpenAI(
            api_key=settings.api_key,
            base_url=settings.base_url,
            max_retries=0,
            timeout=settings.model_timeout_s,
        )

    def complete(self, messages: list[ChatMessage], tools: list[dict[str, Any]]) -> ModelResponse:
        try:
            response = self._client.chat.completions.create(
                **_chat_completion_request(self.settings, messages, tools),
            )
            if not response.choices:
                raise ModelClientError("Model returned no choices", retryable=True)
            message = response.choices[0].message
            calls = tuple(
                ToolCall(id=call.id, name=call.function.name, arguments=call.function.arguments)
                for call in (message.tool_calls or [])
            )
            reasoning_content = _reasoning_content_from_provider_message(message)
            if self.settings.thinking_mode == "enabled" and calls and not reasoning_content:
                raise ModelClientError(
                    "DeepSeek thinking-mode tool call omitted required reasoning_content", retryable=False
                )
            return ModelResponse(
                content=message.content,
                tool_calls=calls,
                model=response.model,
                reasoning_content=reasoning_content,
                usage=_usage_from_response(response),
            )
        except ModelClientError:
            raise
        except Exception as error:
            status_code = getattr(error, "status_code", None)
            retryable = status_code is None or status_code == 429 or status_code >= 500
            raise ModelClientError(f"{type(error).__name__}: {error}", retryable=retryable) from error

    def complete_stream(self, messages: list[ChatMessage], tools: list[dict[str, Any]]):
        """Stream a response, yielding incremental events and a final assembled ModelResponse.

        The final 'done' event carries the accumulated ModelResponse; content/tool-call
        deltas are emitted so the caller can render progress without re-parsing the stream.
        """

        try:
            stream = self._client.chat.completions.create(
                **_chat_completion_request(self.settings, messages, tools),
                stream=True,
                stream_options={"include_usage": True},
            )
        except Exception as error:
            status_code = getattr(error, "status_code", None)
            retryable = status_code is None or status_code == 429 or status_code >= 500
            raise ModelClientError(
                f"{type(error).__name__}: {error}", retryable=retryable,
                partial_text="".join(content_parts) or None,
                partial_reasoning="".join(reasoning_parts) or None,
            ) from error

        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_slots: dict[int, dict[str, str]] = {}
        usage: Usage | None = None
        provider_model: str | None = getattr(stream, "model", None)
        try:
            for chunk in stream:
                chunk_usage = _usage_from_response(chunk)
                if chunk_usage is not None:
                    usage = chunk_usage
                if not getattr(chunk, "choices", None):
                    continue
                delta = chunk.choices[0].delta
                text = getattr(delta, "content", None)
                if isinstance(text, str) and text:
                    content_parts.append(text)
                    yield StreamEvent(kind="content_delta", text=text)
                reasoning = _delta_reasoning(delta)
                if isinstance(reasoning, str) and reasoning:
                    reasoning_parts.append(reasoning)
                    yield StreamEvent(kind="reasoning_delta", text=reasoning)
                for tool_call in (getattr(delta, "tool_calls", None) or []):
                    index = int(getattr(tool_call, "index", 0) or 0)
                    slot = tool_slots.setdefault(index, {"id": "", "name": "", "arguments": ""})
                    if getattr(tool_call, "id", None):
                        slot["id"] = tool_call.id
                    function = getattr(tool_call, "function", None)
                    if function is not None:
                        if getattr(function, "name", None):
                            slot["name"] += function.name or ""
                        if getattr(function, "arguments", None):
                            slot["arguments"] += function.arguments or ""
                    yield StreamEvent(
                        kind="tool_call_delta", index=index, call_id=slot["id"],
                        name=slot["name"], arguments=slot["arguments"],
                    )
        except Exception as error:
            status_code = getattr(error, "status_code", None)
            retryable = status_code is None or status_code == 429 or status_code >= 500
            raise ModelClientError(f"{type(error).__name__}: {error}", retryable=retryable) from error

        calls = tuple(
            ToolCall(id=slot["id"] or f"call_{index}", name=slot["name"], arguments=slot["arguments"] or "{}")
            for index, slot in sorted(tool_slots.items())
        )
        assembled = ModelResponse(
            content="".join(content_parts) or None,
            tool_calls=calls,
            model=provider_model,
            reasoning_content="".join(reasoning_parts) or None,
            usage=usage,
        )
        yield StreamEvent(kind="done", response=assembled)


class RetryingModelClient:
    """Make bounded, observable retries without hiding non-retryable configuration errors."""

    def __init__(self, client: ModelClient, *, retries: int, sleeper: Any = time.sleep) -> None:
        self.client = client
        self.retries = retries
        self.sleeper = sleeper

    def complete(self, messages: list[ChatMessage], tools: list[dict[str, Any]]) -> ModelResponse:
        last_error: ModelClientError | None = None
        for attempt in range(self.retries + 1):
            try:
                return self.client.complete(messages, tools)
            except ModelClientError as error:
                last_error = error
                if not error.retryable or attempt == self.retries:
                    raise
                self.sleeper(0.5 * (2**attempt))
        assert last_error is not None
        raise last_error

    def complete_stream(self, messages: list[ChatMessage], tools: list[dict[str, Any]]):
        """Retry only streams that failed before emitting a delta.

        Replaying a partially rendered response duplicates text/tool deltas and
        can corrupt a UI transcript, so such failures are surfaced with their
        partial text for persistence and recovery instead of being retried.
        """

        inner = getattr(self.client, "complete_stream", None)
        if callable(inner):
            last_error: ModelClientError | None = None
            for attempt in range(self.retries + 1):
                emitted = False
                try:
                    for event in inner(messages, tools):
                        emitted = True
                        yield event
                    return
                except ModelClientError as error:
                    last_error = error
                    emitted = emitted or bool(error.partial_text or error.partial_reasoning)
                    if emitted or not error.retryable or attempt == self.retries:
                        raise
                    self.sleeper(0.5 * (2**attempt))
            assert last_error is not None
            raise last_error
        # Fall back to a non-streaming single event for clients without streaming.
        response = self.complete(messages, tools)
        yield StreamEvent(kind="done", response=response)
