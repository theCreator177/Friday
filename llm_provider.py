"""LLM provider selection for JARVIS.

JARVIS speaks the Anthropic Messages API throughout: every call site does
`client.messages.create(model=..., max_tokens=..., system=..., messages=[...])`
and reads `response.content[0].text`. This module lets a local model served by
LM Studio stand in for that client without touching any of those call sites.

Why a shim rather than a rewrite: the call sites hardcode Claude model ids
(`claude-haiku-4-5-20251001` for fast turns, `claude-opus-4-6` for research).
The shim intercepts those ids and maps them onto whichever local models you
have loaded, so the routing intent — fast vs. deep — is preserved.

Configuration (all via environment / .env):

    LLM_PROVIDER        anthropic | local | auto   (default: auto)
    LOCAL_LLM_BASE_URL  default http://localhost:1234/v1
    LOCAL_LLM_MODEL     model id for ordinary turns
    LOCAL_LLM_DEEP_MODEL  optional; model id for research turns
    LOCAL_LLM_API_KEY   default "lm-studio" (LM Studio ignores the value)
    LOCAL_LLM_TIMEOUT   seconds, default 120

`auto` picks local when LOCAL_LLM_MODEL is set, otherwise Anthropic.

Self-check:

    python llm_provider.py --check
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

log = logging.getLogger("jarvis.llm")

DEFAULT_BASE_URL = "http://localhost:1234/v1"
DEFAULT_TIMEOUT = 120.0

# Call sites ask for Claude ids. Anything matching these substrings is treated
# as a "deep" request and routed to LOCAL_LLM_DEEP_MODEL when one is set.
_DEEP_MODEL_HINTS = ("opus", "sonnet")


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        log.warning("%s=%r is not a number; using %s", name, raw, default)
        return default


# ---------------------------------------------------------------------------
# Anthropic-shaped response objects
# ---------------------------------------------------------------------------
# server.py reads response.content[0].text and track_usage() reads
# response.usage.input_tokens / .output_tokens. These mirror that surface so
# existing code cannot tell the difference.


@dataclass
class _TextBlock:
    text: str
    type: str = "text"


@dataclass
class _Usage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class _LocalResponse:
    content: list[_TextBlock]
    usage: _Usage = field(default_factory=_Usage)
    model: str = ""
    stop_reason: Optional[str] = None
    role: str = "assistant"


# ---------------------------------------------------------------------------
# Message translation
# ---------------------------------------------------------------------------


def _flatten_content(content: Any) -> str:
    """Reduce Anthropic-style content to plain text.

    Content arrives either as a bare string or as a list of blocks (dicts with
    a "text" key, or objects with a .text attribute). Non-text blocks such as
    images have no OpenAI chat-completions equivalent here and are dropped.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                if block.get("type", "text") == "text" and block.get("text"):
                    parts.append(block["text"])
            else:
                text = getattr(block, "text", None)
                if text:
                    parts.append(text)
        return "\n".join(parts)
    return "" if content is None else str(content)


def to_openai_messages(system: Any, messages: list[dict]) -> list[dict]:
    """Convert an Anthropic system prompt + message list to OpenAI chat format."""
    out: list[dict] = []
    system_text = _flatten_content(system)
    if system_text:
        out.append({"role": "system", "content": system_text})
    for msg in messages:
        role = msg.get("role", "user")
        if role not in ("user", "assistant", "system"):
            role = "user"
        out.append({"role": role, "content": _flatten_content(msg.get("content"))})
    return out


# ---------------------------------------------------------------------------
# Local client
# ---------------------------------------------------------------------------


class _LocalMessages:
    """Implements the `.messages.create(...)` half of the Anthropic client."""

    def __init__(self, client: "LocalLLMClient"):
        self._client = client

    async def create(
        self,
        *,
        model: str = "",
        max_tokens: int = 1024,
        system: Any = "",
        messages: Optional[list[dict]] = None,
        temperature: Optional[float] = None,
        **_ignored: Any,
    ) -> _LocalResponse:
        target = self._client.resolve_model(model)
        payload: dict[str, Any] = {
            "model": target,
            "messages": to_openai_messages(system, messages or []),
            "max_tokens": max_tokens,
            "stream": False,
        }
        if temperature is not None:
            payload["temperature"] = temperature

        data = await self._client.post_json("/chat/completions", payload)

        choices = data.get("choices") or []
        text = ""
        finish_reason = None
        if choices:
            finish_reason = choices[0].get("finish_reason")
            text = (choices[0].get("message") or {}).get("content") or ""

        usage = data.get("usage") or {}
        return _LocalResponse(
            content=[_TextBlock(text=text)],
            usage=_Usage(
                input_tokens=int(usage.get("prompt_tokens") or 0),
                output_tokens=int(usage.get("completion_tokens") or 0),
            ),
            model=data.get("model") or target,
            stop_reason=finish_reason,
        )


class LocalLLMClient:
    """Drop-in stand-in for anthropic.AsyncAnthropic backed by an
    OpenAI-compatible server (LM Studio, llama.cpp, Ollama's compat endpoint)."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        model: str = "",
        deep_model: str = "",
        api_key: str = "lm-studio",
        timeout: float = DEFAULT_TIMEOUT,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.deep_model = deep_model or model
        self.api_key = api_key or "lm-studio"
        self.timeout = timeout
        self.messages = _LocalMessages(self)

    def resolve_model(self, requested: str) -> str:
        """Map a Claude model id from a call site onto a local model."""
        name = (requested or "").lower()
        if any(hint in name for hint in _DEEP_MODEL_HINTS):
            return self.deep_model
        return self.model

    async def post_json(self, path: str, payload: dict) -> dict:
        url = f"{self.base_url}{path}"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        async with httpx.AsyncClient(timeout=self.timeout) as http:
            try:
                resp = await http.post(url, json=payload, headers=headers)
            except httpx.ConnectError as exc:
                raise RuntimeError(
                    f"Cannot reach the local model server at {self.base_url}. "
                    "Is LM Studio running with its server started?"
                ) from exc
            if resp.status_code >= 400:
                # LM Studio returns 4xx when the `model` field doesn't match an
                # id from /v1/models — the most common misconfiguration.
                raise RuntimeError(
                    f"Local model server returned {resp.status_code}: {resp.text[:300]}"
                )
            return resp.json()

    async def list_models(self) -> list[str]:
        url = f"{self.base_url}/models"
        async with httpx.AsyncClient(timeout=min(self.timeout, 30.0)) as http:
            resp = await http.get(url, headers={"Authorization": f"Bearer {self.api_key}"})
            resp.raise_for_status()
            return [m.get("id", "") for m in (resp.json().get("data") or [])]


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


def provider_name() -> str:
    """Resolve which provider to use: 'local' or 'anthropic'."""
    choice = _env("LLM_PROVIDER", "auto").lower()
    if choice == "local":
        return "local"
    if choice == "anthropic":
        return "anthropic"
    if choice and choice != "auto":
        log.warning("LLM_PROVIDER=%r not recognised; falling back to auto", choice)
    return "local" if _env("LOCAL_LLM_MODEL") else "anthropic"


def build_client(anthropic_api_key: str = "") -> tuple[Optional[Any], str]:
    """Return (client, provider_name). Client is None when unconfigured."""
    provider = provider_name()

    if provider == "local":
        model = _env("LOCAL_LLM_MODEL")
        if not model:
            log.error("LLM_PROVIDER=local but LOCAL_LLM_MODEL is not set")
            return None, "local"
        client = LocalLLMClient(
            base_url=_env("LOCAL_LLM_BASE_URL", DEFAULT_BASE_URL),
            model=model,
            deep_model=_env("LOCAL_LLM_DEEP_MODEL"),
            api_key=_env("LOCAL_LLM_API_KEY", "lm-studio"),
            timeout=_env_float("LOCAL_LLM_TIMEOUT", DEFAULT_TIMEOUT),
        )
        log.info("LLM provider: local (%s, model=%s)", client.base_url, model)
        return client, "local"

    if not anthropic_api_key:
        return None, "anthropic"

    import anthropic  # imported lazily so a local-only setup needn't install it

    log.info("LLM provider: anthropic")
    return anthropic.AsyncAnthropic(api_key=anthropic_api_key), "anthropic"


# ---------------------------------------------------------------------------
# CLI self-check
# ---------------------------------------------------------------------------


async def _check() -> int:
    provider = provider_name()
    print(f"provider     : {provider}")

    if provider != "local":
        key = _env("ANTHROPIC_API_KEY")
        print(f"api key      : {'set' if key else 'MISSING'}")
        if not key:
            print("\nSet ANTHROPIC_API_KEY, or set LOCAL_LLM_MODEL to use a local model.")
            return 1
        return 0

    base = _env("LOCAL_LLM_BASE_URL", DEFAULT_BASE_URL)
    model = _env("LOCAL_LLM_MODEL")
    print(f"base url     : {base}")
    print(f"model        : {model or 'MISSING'}")
    print(f"deep model   : {_env('LOCAL_LLM_DEEP_MODEL') or '(same as model)'}")

    if not model:
        print("\nLOCAL_LLM_MODEL is not set. Run `lms ps` to see loaded models.")
        return 1

    client = LocalLLMClient(
        base_url=base,
        model=model,
        deep_model=_env("LOCAL_LLM_DEEP_MODEL"),
        api_key=_env("LOCAL_LLM_API_KEY", "lm-studio"),
        timeout=_env_float("LOCAL_LLM_TIMEOUT", DEFAULT_TIMEOUT),
    )

    try:
        available = await client.list_models()
    except Exception as exc:  # noqa: BLE001 — surface any reachability failure
        print(f"\nFAIL: cannot reach {base} — {exc}")
        print("Start LM Studio and run `lms server start`.")
        return 1

    print(f"loaded models: {', '.join(available) or '(none)'}")
    if model not in available:
        print(f"\nFAIL: '{model}' is not loaded. Use one of the ids above verbatim.")
        return 1

    try:
        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",  # exercises the mapping path
            max_tokens=32,
            system="Reply with exactly: OK",
            messages=[{"role": "user", "content": "Say OK."}],
        )
    except Exception as exc:  # noqa: BLE001
        print(f"\nFAIL: completion failed — {exc}")
        return 1

    print(f"test reply   : {resp.content[0].text.strip()[:80]!r}")
    print(f"tokens       : in={resp.usage.input_tokens} out={resp.usage.output_tokens}")
    print("\nOK — JARVIS can use this local model.")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if "--check" in sys.argv:
        sys.exit(asyncio.run(_check()))
    print(__doc__)
