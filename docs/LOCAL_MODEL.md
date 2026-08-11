# Running JARVIS on a local model

JARVIS normally calls the Anthropic API. This document covers pointing it at a
model running on your own machine through LM Studio instead — a Kimi GGUF, or
anything else LM Studio can serve.

## What this does and doesn't change

**Does:** JARVIS's conversation turns, intent classification, memory extraction,
and research calls run against your local model. No tokens leave your machine
and there is no per-token cost.

**Doesn't:** The Claude Code CLI stays on Claude. Anthropic
[does not support routing Claude Code to non-Claude models](https://code.claude.com/docs/en/llm-gateway),
so JARVIS's `[ACTION:BUILD]` path — which shells out to `claude` — keeps using
your Claude account. Local model for the voice assistant, Claude for the coding
agent. They're independent, and both can run at once.

## The network constraint

The model has to be reachable from wherever `server.py` runs.

LM Studio binds to `localhost`. A browser-based cloud shell is a container in a
datacenter — it has no route to your desktop's `localhost`, and no amount of
configuration changes that. So:

| Where `server.py` runs | Local model reachable? |
|---|---|
| Same machine as LM Studio | Yes — this is the supported setup |
| Another machine on your LAN | Yes, if LM Studio serves on `0.0.0.0` and the firewall allows it |
| Cloud shell / remote container | No, unless you deliberately expose the endpoint |

Exposing a local inference endpoint to the internet through a tunnel is
possible but puts an unauthenticated model server on a public URL. If you go
that route, put authentication in front of it. Running JARVIS on the same
machine as the model avoids the problem entirely.

## Setup

### 1. Serve the model

In LM Studio: **My Models** confirms where your `.gguf` files live (the models
directory is set in the app — there is no CLI flag for it). Load a model, then
**Developer → Start server**.

Equivalently from a terminal:

```powershell
lms server start
lms ls        # models on disk
lms load <model> --gpu=max
lms ps        # what is currently loaded
```

### 2. Point JARVIS at it

From the repo root on Windows:

```powershell
.\scripts\lmstudio-bootstrap.ps1
```

The script verifies the models directory, starts the server, confirms a model is
loaded, sends a real test completion, and writes the `LOCAL_LLM_*` values into
`.env`. Override the defaults if needed:

```powershell
.\scripts\lmstudio-bootstrap.ps1 -ModelsDir 'D:\Tony6-Home\.lmstudio\models' -Model 'kimi-k2-instruct' -Port 1234
```

To configure it by hand instead, in `.env`:

```ini
LLM_PROVIDER=local
LOCAL_LLM_BASE_URL=http://localhost:1234/v1
LOCAL_LLM_MODEL=kimi-k2-instruct
```

### 3. Verify

```bash
python llm_provider.py --check
```

This prints the resolved provider, lists the models the server reports, sends a
completion, and exits non-zero on failure — so it works in a script. Then start
JARVIS as usual with `python server.py`.

## Configuration reference

| Variable | Default | Meaning |
|---|---|---|
| `LLM_PROVIDER` | `auto` | `anthropic`, `local`, or `auto` (local when `LOCAL_LLM_MODEL` is set) |
| `LOCAL_LLM_BASE_URL` | `http://localhost:1234/v1` | OpenAI-compatible base URL; `/v1` suffix required |
| `LOCAL_LLM_MODEL` | — | Model id for ordinary turns |
| `LOCAL_LLM_DEEP_MODEL` | falls back to `LOCAL_LLM_MODEL` | Model id for research turns |
| `LOCAL_LLM_API_KEY` | `lm-studio` | LM Studio ignores the value, but the header must exist |
| `LOCAL_LLM_TIMEOUT` | `120` | Seconds. Raise it on slower hardware |

## How it works

Every JARVIS call site speaks the Anthropic Messages API:

```python
response = await client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=250, system=system, messages=messages,
)
return response.content[0].text
```

`llm_provider.LocalLLMClient` implements that exact surface on top of
`/v1/chat/completions`, so no call site changes. It:

- folds the Anthropic `system` parameter into a leading `system` message, since
  OpenAI-format servers take it inline;
- flattens content blocks to text (image blocks are dropped — a local text model
  has nothing to do with them);
- returns an object exposing `.content[0].text` and
  `.usage.input_tokens` / `.usage.output_tokens`, which is what `track_usage()`
  reads;
- maps the hardcoded Claude ids onto your local models — anything containing
  `opus` or `sonnet` routes to `LOCAL_LLM_DEEP_MODEL`, everything else to
  `LOCAL_LLM_MODEL`. The fast/deep routing intent survives even though the
  Claude ids don't.

Cost reporting returns `$0.00` while the local provider is active, so the usage
summary doesn't quote API pricing for inference that was free.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `Cannot reach the local model server` | Server not started, or wrong port | `lms server start`; check the port matches |
| HTTP 400 / `model not found` | `LOCAL_LLM_MODEL` doesn't match a served id | Copy the id from `GET /v1/models` verbatim — it usually has a quantization suffix |
| `no model is loaded` | Server up, nothing loaded | `lms load <model>` |
| Responses time out | Large model on modest hardware | Raise `LOCAL_LLM_TIMEOUT`; try `lms load --gpu=max` |
| JARVIS still uses Anthropic | `LLM_PROVIDER` unset with no `LOCAL_LLM_MODEL` | Set both explicitly; confirm with `--check` |
| Replies ignore the JARVIS persona | Small models follow long system prompts loosely | Use a larger/instruct-tuned quant |

## Tests

```bash
python -m pytest tests/test_llm_provider.py -q
```

Covers message translation, the response surface `server.py` depends on, model
tier mapping, provider selection, and error paths.
