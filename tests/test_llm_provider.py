"""Tests for the local-model provider shim.

The point of llm_provider is that a local model can impersonate the Anthropic
client closely enough that server.py never notices. These tests pin that
contract: message translation, the response surface server.py reads, model
tier mapping, and provider selection.
"""

import asyncio
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import llm_provider  # noqa: E402
from llm_provider import (  # noqa: E402
    LocalLLMClient,
    _flatten_content,
    build_client,
    provider_name,
    to_openai_messages,
)


def run(coro):
    # asyncio.run closes the loop it creates; hand-rolled new_event_loop() calls
    # leak one per test and surface as "coroutine was never awaited" warnings.
    return asyncio.run(coro)


class FlattenContentTest(unittest.TestCase):
    def test_plain_string_passes_through(self):
        self.assertEqual(_flatten_content("hello"), "hello")

    def test_block_list_is_joined(self):
        blocks = [{"type": "text", "text": "one"}, {"type": "text", "text": "two"}]
        self.assertEqual(_flatten_content(blocks), "one\ntwo")

    def test_non_text_blocks_are_dropped(self):
        blocks = [{"type": "image", "source": {}}, {"type": "text", "text": "kept"}]
        self.assertEqual(_flatten_content(blocks), "kept")

    def test_none_becomes_empty(self):
        self.assertEqual(_flatten_content(None), "")

    def test_objects_with_text_attribute(self):
        class Block:
            text = "attr"

        self.assertEqual(_flatten_content([Block()]), "attr")


class MessageTranslationTest(unittest.TestCase):
    def test_system_prompt_becomes_leading_system_message(self):
        out = to_openai_messages("be terse", [{"role": "user", "content": "hi"}])
        self.assertEqual(out[0], {"role": "system", "content": "be terse"})
        self.assertEqual(out[1], {"role": "user", "content": "hi"})

    def test_empty_system_is_omitted(self):
        out = to_openai_messages("", [{"role": "user", "content": "hi"}])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["role"], "user")

    def test_unknown_roles_fall_back_to_user(self):
        out = to_openai_messages("", [{"role": "tool", "content": "x"}])
        self.assertEqual(out[0]["role"], "user")

    def test_assistant_turns_are_preserved(self):
        history = [
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
        ]
        self.assertEqual([m["role"] for m in to_openai_messages("", history)],
                         ["user", "assistant"])


class ModelMappingTest(unittest.TestCase):
    def setUp(self):
        self.client = LocalLLMClient(model="fast-local", deep_model="deep-local")

    def test_haiku_maps_to_fast_model(self):
        self.assertEqual(self.client.resolve_model("claude-haiku-4-5-20251001"), "fast-local")

    def test_opus_maps_to_deep_model(self):
        self.assertEqual(self.client.resolve_model("claude-opus-4-6"), "deep-local")

    def test_deep_falls_back_to_fast_when_unset(self):
        client = LocalLLMClient(model="only-one")
        self.assertEqual(client.resolve_model("claude-opus-4-6"), "only-one")

    def test_unknown_model_id_uses_fast(self):
        self.assertEqual(self.client.resolve_model(""), "fast-local")


class ResponseSurfaceTest(unittest.TestCase):
    """server.py reads response.content[0].text and response.usage.*_tokens."""

    def setUp(self):
        self.client = LocalLLMClient(model="kimi-local")
        self.payload = {
            "model": "kimi-local",
            "choices": [{"message": {"content": "Will do, sir."}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 4},
        }

    def _create(self, **kwargs):
        with mock.patch.object(
            LocalLLMClient, "post_json", new=mock.AsyncMock(return_value=self.payload)
        ):
            return run(self.client.messages.create(**kwargs))

    def test_text_is_reachable_the_anthropic_way(self):
        resp = self._create(model="claude-haiku-4-5-20251001",
                            messages=[{"role": "user", "content": "hi"}])
        self.assertEqual(resp.content[0].text, "Will do, sir.")

    def test_usage_fields_match_anthropic_names(self):
        resp = self._create(model="x", messages=[{"role": "user", "content": "hi"}])
        self.assertEqual(resp.usage.input_tokens, 11)
        self.assertEqual(resp.usage.output_tokens, 4)

    def test_missing_usage_defaults_to_zero(self):
        self.payload = {"choices": [{"message": {"content": "hi"}}]}
        resp = self._create(model="x", messages=[])
        self.assertEqual(resp.usage.input_tokens, 0)
        self.assertEqual(resp.usage.output_tokens, 0)

    def test_empty_choices_yields_empty_text_not_crash(self):
        self.payload = {"choices": []}
        resp = self._create(model="x", messages=[])
        self.assertEqual(resp.content[0].text, "")

    def test_request_payload_uses_resolved_model(self):
        captured = {}

        async def fake_post(self_, path, payload):
            captured.update(payload)
            return {"choices": [{"message": {"content": "ok"}}]}

        with mock.patch.object(LocalLLMClient, "post_json", new=fake_post):
            client = LocalLLMClient(model="fast-local", deep_model="deep-local")
            run(client.messages.create(
                model="claude-opus-4-6",
                max_tokens=99,
                system="sys",
                messages=[{"role": "user", "content": "q"}],
            ))

        self.assertEqual(captured["model"], "deep-local")
        self.assertEqual(captured["max_tokens"], 99)
        self.assertEqual(captured["messages"][0], {"role": "system", "content": "sys"})
        self.assertFalse(captured["stream"])


class ProviderSelectionTest(unittest.TestCase):
    def _env(self, **kwargs):
        clean = {k: "" for k in
                 ("LLM_PROVIDER", "LOCAL_LLM_MODEL", "LOCAL_LLM_BASE_URL",
                  "LOCAL_LLM_DEEP_MODEL", "ANTHROPIC_API_KEY")}
        clean.update(kwargs)
        return mock.patch.dict(os.environ, clean, clear=False)

    def test_auto_picks_local_when_model_set(self):
        with self._env(LOCAL_LLM_MODEL="kimi"):
            self.assertEqual(provider_name(), "local")

    def test_auto_picks_anthropic_without_local_model(self):
        with self._env():
            self.assertEqual(provider_name(), "anthropic")

    def test_explicit_local_is_honoured(self):
        with self._env(LLM_PROVIDER="local", LOCAL_LLM_MODEL="kimi"):
            self.assertEqual(provider_name(), "local")

    def test_explicit_anthropic_overrides_local_model(self):
        with self._env(LLM_PROVIDER="anthropic", LOCAL_LLM_MODEL="kimi"):
            self.assertEqual(provider_name(), "anthropic")

    def test_unrecognised_value_falls_back_to_auto(self):
        with self._env(LLM_PROVIDER="banana", LOCAL_LLM_MODEL="kimi"):
            self.assertEqual(provider_name(), "local")

    def test_build_client_returns_local_client(self):
        with self._env(LOCAL_LLM_MODEL="kimi", LOCAL_LLM_BASE_URL="http://x:1234/v1"):
            client, name = build_client("")
        self.assertEqual(name, "local")
        self.assertIsInstance(client, LocalLLMClient)
        self.assertEqual(client.base_url, "http://x:1234/v1")
        self.assertEqual(client.model, "kimi")

    def test_build_client_local_without_model_returns_none(self):
        with self._env(LLM_PROVIDER="local"):
            client, name = build_client("")
        self.assertIsNone(client)
        self.assertEqual(name, "local")

    def test_build_client_anthropic_without_key_returns_none(self):
        with self._env(LLM_PROVIDER="anthropic"):
            client, name = build_client("")
        self.assertIsNone(client)
        self.assertEqual(name, "anthropic")

    def test_trailing_slash_stripped_from_base_url(self):
        client = LocalLLMClient(base_url="http://localhost:1234/v1/", model="m")
        self.assertEqual(client.base_url, "http://localhost:1234/v1")


class TimeoutParsingTest(unittest.TestCase):
    def test_bad_timeout_falls_back_to_default(self):
        with mock.patch.dict(os.environ, {"LOCAL_LLM_TIMEOUT": "not-a-number"}):
            self.assertEqual(
                llm_provider._env_float("LOCAL_LLM_TIMEOUT", 120.0), 120.0
            )

    def test_valid_timeout_is_used(self):
        with mock.patch.dict(os.environ, {"LOCAL_LLM_TIMEOUT": "45"}):
            self.assertEqual(llm_provider._env_float("LOCAL_LLM_TIMEOUT", 120.0), 45.0)


class ErrorHandlingTest(unittest.TestCase):
    def test_connect_error_names_the_server(self):
        import httpx

        client = LocalLLMClient(base_url="http://localhost:9/v1", model="m")
        with mock.patch("httpx.AsyncClient.post",
                        new=mock.AsyncMock(side_effect=httpx.ConnectError("refused"))):
            with self.assertRaises(RuntimeError) as ctx:
                run(client.post_json("/chat/completions", {}))
        self.assertIn("localhost:9", str(ctx.exception))
        self.assertIn("LM Studio", str(ctx.exception))

    def test_http_error_surfaces_status_and_body(self):
        client = LocalLLMClient(model="m")
        resp = mock.Mock(status_code=404, text="model not found")
        with mock.patch("httpx.AsyncClient.post", new=mock.AsyncMock(return_value=resp)):
            with self.assertRaises(RuntimeError) as ctx:
                run(client.post_json("/chat/completions", {}))
        self.assertIn("404", str(ctx.exception))
        self.assertIn("model not found", str(ctx.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
