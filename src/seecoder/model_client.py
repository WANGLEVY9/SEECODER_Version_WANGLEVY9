"""A narrow adapter around an OpenAI-compatible native tool-calling API."""

from __future__ import annotations

import time
from typing import Any, Protocol

from seecoder.config import Settings
from seecoder.types import ChatMessage, ModelResponse, ToolCall


class ModelClientError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class ModelClient(Protocol):
    def complete(self, messages: list[ChatMessage], tools: list[dict[str, Any]]) -> ModelResponse: ...


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
    request: dict[str, Any] = {
        "model": settings.model,
        "messages": [_as_openai_message(message) for message in messages],
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
        self._client = OpenAI(api_key=settings.api_key, base_url=settings.base_url, max_retries=0)

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
            )
        except ModelClientError:
            raise
        except Exception as error:
            status_code = getattr(error, "status_code", None)
            retryable = status_code is None or status_code == 429 or status_code >= 500
            raise ModelClientError(f"{type(error).__name__}: {error}", retryable=retryable) from error


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
