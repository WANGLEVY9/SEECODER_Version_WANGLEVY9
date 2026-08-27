from __future__ import annotations

import unittest
from typing import Any

from seecoder.config import Settings
from seecoder.model_client import (
    ModelClientError,
    RetryingModelClient,
    _as_openai_message,
    _chat_completion_request,
    _reasoning_content_from_provider_message,
    _usage_from_response,
)
from seecoder.types import ChatMessage, ModelResponse, ToolCall


class FlakyClient:
    def __init__(self) -> None:
        self.attempts = 0

    def complete(self, messages: list[ChatMessage], tools: list[dict[str, Any]]) -> ModelResponse:  # noqa: ARG002
        self.attempts += 1
        if self.attempts == 1:
            raise ModelClientError("temporary outage", retryable=True)
        return ModelResponse("done")


class ExtraOnlyProviderMessage:
    reasoning_content = None
    model_extra = {"reasoning_content": "provider extension reasoning"}


class ModelClientTests(unittest.TestCase):
    def test_native_tool_call_is_serialized_in_openai_shape(self) -> None:
        message = ChatMessage(
            role="assistant",
            content=None,
            tool_calls=(ToolCall(id="call-1", name="read_file", arguments='{"path":"a.py"}'),),
        )
        serialized = _as_openai_message(message)
        self.assertEqual(serialized["tool_calls"][0]["type"], "function")
        self.assertEqual(serialized["tool_calls"][0]["function"]["name"], "read_file")

    def test_reasoning_content_is_preserved_in_provider_message(self) -> None:
        serialized = _as_openai_message(
            ChatMessage(role="assistant", content=None, reasoning_content="private continuation state")
        )
        self.assertEqual(serialized["reasoning_content"], "private continuation state")

    def test_only_retryable_model_errors_are_retried(self) -> None:
        client = FlakyClient()
        response = RetryingModelClient(client, retries=2, sleeper=lambda _: None).complete([], [])
        self.assertEqual(response.content, "done")
        self.assertEqual(client.attempts, 2)

    def test_deepseek_thinking_mode_is_explicitly_forwarded(self) -> None:
        request = _chat_completion_request(
            Settings(api_key="test", model="deepseek-v4-flash", thinking_mode="disabled"), [], []
        )
        self.assertEqual(request["extra_body"], {"thinking": {"type": "disabled"}})

    def test_enabled_thinking_includes_reasoning_effort(self) -> None:
        request = _chat_completion_request(
            Settings(
                api_key="test",
                model="deepseek-v4-flash",
                thinking_mode="enabled",
                reasoning_effort="max",
            ),
            [],
            [],
        )
        self.assertEqual(request["reasoning_effort"], "max")

    def test_deepseek_reasoning_extension_is_read_from_model_extra(self) -> None:
        self.assertEqual(
            _reasoning_content_from_provider_message(ExtraOnlyProviderMessage()), "provider extension reasoning"
        )

    def test_provider_default_does_not_send_deepseek_specific_parameters(self) -> None:
        request = _chat_completion_request(Settings(api_key="test", model="generic"), [], [])
        self.assertNotIn("extra_body", request)

    def test_usage_is_extracted_from_provider_response(self) -> None:
        class ProviderUsage:
            prompt_tokens = 12
            completion_tokens = 8
            total_tokens = 20

        class ProviderResponse:
            usage = ProviderUsage()

        usage = _usage_from_response(ProviderResponse())
        self.assertEqual(usage.prompt_tokens, 12)
        self.assertEqual(usage.completion_tokens, 8)
        self.assertEqual(usage.total_tokens, 20)
