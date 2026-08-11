# JARVIS — Voice AI Assistant

## Overview
JARVIS (Just A Rather Very Intelligent System) is a voice-first AI assistant for macOS. It runs locally on your machine, connecting to your Apple Calendar, Mail, Notes, and can spawn Claude Code sessions for development tasks.

## Quick Start
When a user clones this repo and starts Claude Code, help them:
1. Copy .env.example to .env
2. Get an Anthropic API key from console.anthropic.com
3. Get a Fish Audio API key from fish.audio
4. Install Python dependencies: pip install -r requirements.txt
5. Install frontend dependencies: cd frontend && npm install
6. Generate SSL certs: openssl req -x509 -newkey rsa:2048 -keyout key.pem -out cert.pem -days 365 -nodes -subj '/CN=localhost'
7. Run the backend: python server.py
8. Run the frontend: cd frontend && npm run dev
9. Open Chrome to http://localhost:5173
10. Click to enable audio, speak to JARVIS

## Architecture
- **Backend**: FastAPI + Python (server.py, ~2300 lines)
- **Frontend**: Vite + TypeScript + Three.js (audio-reactive orb)
- **Communication**: WebSocket (JSON messages + binary audio)
- **AI**: Claude Haiku for fast responses, Claude Opus for research
- **TTS**: Fish Audio with JARVIS voice model
- **System**: AppleScript for Calendar, Mail, Notes, Terminal integration

## Key Files
- `server.py` — Main server, WebSocket handler, LLM integration, action system
- `frontend/src/orb.ts` — Three.js particle orb visualization
- `frontend/src/voice.ts` — Web Speech API + audio playback
- `frontend/src/main.ts` — Frontend state machine
- `memory.py` — SQLite memory system with FTS5 search
- `calendar_access.py` — Apple Calendar integration via AppleScript
- `mail_access.py` — Apple Mail integration (READ-ONLY)
- `notes_access.py` — Apple Notes integration
- `actions.py` — System actions (Terminal, Chrome, Claude Code)
- `browser.py` — Playwright web automation
- `hermes.py` — Reach layer: URL → readable text (cross-platform, no AppleScript)
- `work_mode.py` — Persistent Claude Code sessions

## Environment Variables
- `ANTHROPIC_API_KEY` (required) — Claude API access
- `FISH_API_KEY` (required) — Fish Audio TTS
- `FISH_VOICE_ID` (optional) — Voice model ID
- `USER_NAME` (optional) — Your name for JARVIS to use
- `CALENDAR_ACCOUNTS` (optional) — Comma-separated calendar emails

## Reach (hermes.py)
Reads a URL and brings the text back into the conversation, so JARVIS can answer
about a page instead of only opening one. `[ACTION:BROWSE]` puts a page on screen;
`[ACTION:REACH]` puts it in his head.

- Routes URLs to a platform (GitHub, X, YouTube, Reddit, LinkedIn, …) using the
  optional [Agent Reach](https://github.com/Panniantong/Agent-Reach) channel table.
- Agent Reach is an *installer and doctor*, not a fetch library — its channels
  answer `can_handle()`/`check()` and, bar one, do not read. Hermes is the runtime
  layer it deliberately omits.
- Reads through a channel's own backend when one is live; otherwise Jina Reader,
  which needs no key and handles any URL. Reach is therefore never zero.
- Optional install for richer backends: `pip install agent-reach` (then
  `agent-reach doctor`). Without it, routing uses a built-in host table.
- `GET /api/reach` reports which channels are live, on fallback, or unavailable.
- Pure Python + httpx — no AppleScript, so this half of the stack already runs on
  macOS, Windows, and Linux.

## Conventions
- JARVIS personality: British butler, dry wit, economy of language
- Max 1-2 sentences per voice response
- Action tags: [ACTION:BUILD], [ACTION:BROWSE], [ACTION:REACH], [ACTION:RESEARCH], etc.
- AppleScript for all macOS integrations (no OAuth needed)
- Read-only for Mail (safety by design)
- SQLite for all local data storage
