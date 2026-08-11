"""
Hermes — Holdfast's reach layer.

Turns a URL into readable text and hands it back to the assistant, so it can
answer questions about a page rather than merely opening one. [ACTION:BROWSE]
puts a page on a screen; Hermes brings the page into the conversation.

Agent Reach (the upstream Agent-Reach project) is an installer and a doctor,
not a fetch library: its channels answer `can_handle(url)` and `check(config)`,
and with a single exception they do not read anything. Hermes supplies the
runtime layer Agent Reach deliberately leaves out. It borrows Agent Reach's
channel table to identify which platform a URL belongs to and to report which
backends are actually live on this machine, then reads by the best route
available:

  1. a channel's own read(), when it has one and its backend is live
  2. Jina Reader — the universal fallback: any URL, no key, nothing to install

Agent Reach is optional. Without it Hermes falls back to a built-in host table
and still reads every URL through Jina, so the assistant is never left with no
reach at all.

Pure stdlib plus httpx, which the server already depends on. No AppleScript and
no platform-specific shell-outs — this behaves identically on macOS, Windows,
and Linux.

Searching deliberately stays with `browser.py`, which already drives Playwright.
Hermes is about reach and retrieval, not about duplicating that.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import asdict, dataclass, field
from urllib.parse import urlsplit

import httpx

log = logging.getLogger("jarvis.hermes")

JINA_ENDPOINT = "https://r.jina.ai/"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

DEFAULT_TIMEOUT = 30.0
DEFAULT_MAX_CHARS = 8000
AVAILABILITY_TTL = 300.0  # channel probes shell out; don't re-run them per request

# Host routing used when Agent Reach isn't importable. Mirrors the upstream
# channels so behaviour degrades in coverage, never in correctness.
_FALLBACK_HOSTS: dict[str, tuple[str, ...]] = {
    "github": ("github.com",),
    "twitter": ("x.com", "twitter.com"),
    "youtube": ("youtube.com", "youtu.be"),
    "reddit": ("reddit.com", "redd.it"),
    "facebook": ("facebook.com", "fb.com", "fb.watch"),
    "instagram": ("instagram.com", "instagr.am"),
    "bilibili": ("bilibili.com", "b23.tv"),
    "xiaohongshu": ("xiaohongshu.com", "xhslink.com"),
    "linkedin": ("linkedin.com",),
    "xiaoyuzhou": ("xiaoyuzhoufm.com",),
    "v2ex": ("v2ex.com",),
    "xueqiu": ("xueqiu.com",),
}

# The catch-all channel. Never the answer while a specific platform still fits.
WEB = "web"


# ---------------------------------------------------------------------------
# URL handling
# ---------------------------------------------------------------------------

def normalize_url(url: str) -> str:
    """Add a scheme to a bare host so voice input like 'github.com/x' resolves."""
    url = (url or "").strip()
    if url and not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


def _domain_matches(host: str, *domains: str) -> bool:
    """Exact host or a real subdomain — never a substring.

    Mirrors Agent Reach's own matcher. Substring matching would accept
    lookalikes such as ``github.com.evil.test``.
    """
    host = str(host or "").lower().strip(".")
    if not host:
        return False
    return any(
        host == d.lower().strip(".") or host.endswith("." + d.lower().strip("."))
        for d in domains
    )


def _host_of(url: str) -> str:
    """Hostname of an http(s) URL, or '' if it is malformed or disguised."""
    try:
        parsed = urlsplit(url)
        _ = parsed.port  # out-of-range ports only raise when accessed — force it
    except (TypeError, ValueError):
        return ""
    if parsed.scheme.lower() not in ("http", "https"):
        return ""
    if parsed.username is not None or parsed.password is not None:
        return ""
    return (parsed.hostname or "").lower().rstrip(".")


# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------

@dataclass
class ReachResult:
    """Outcome of a reach. Always returned — failures arrive as ok=False."""

    url: str
    platform: str = WEB
    title: str = ""
    text: str = ""
    backend: str = ""
    word_count: int = 0
    truncated: bool = False
    ok: bool = True
    error: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    def summary(self) -> str:
        """One line fit for a voice response or a log."""
        if not self.ok:
            return f"Could not reach {self.url}: {self.error}"
        label = self.title or self.url
        return f"{label} — {self.word_count} words via {self.backend}"


@dataclass
class ChannelStatus:
    name: str
    status: str          # ok | warn | off | error
    message: str = ""
    backend: str = ""
    tier: int = 0

    @property
    def available(self) -> bool:
        """Reachable at all. 'warn' counts — upstream uses it for degraded-but-usable."""
        return self.status in ("ok", "warn")

    @property
    def live(self) -> bool:
        """Serving through its own backend, rather than leaning on the fallback."""
        return self.status == "ok" and bool(self.backend)

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Hermes
# ---------------------------------------------------------------------------

class Hermes:
    """Reach: identify a URL's platform, then read it back as text."""

    def __init__(self, timeout: float = DEFAULT_TIMEOUT, max_chars: int = DEFAULT_MAX_CHARS):
        self.timeout = timeout
        self.max_chars = max_chars
        self._client: httpx.AsyncClient | None = None
        self._channels: list = []
        self._config = None
        self._availability: dict[str, ChannelStatus] = {}
        self._availability_at: float = 0.0
        self._load_channels()

    # -- Agent Reach discovery ---------------------------------------------

    def _load_channels(self) -> None:
        """Import Agent Reach if present. Absence is normal, not an error."""
        try:
            from agent_reach.channels import get_all_channels
            from agent_reach.config import Config

            self._channels = list(get_all_channels())
            try:
                self._config = Config()
            except Exception as e:  # a broken config must not cost us routing
                log.warning(f"Agent Reach config unavailable, using defaults: {e}")
                self._config = None
            log.info(f"Hermes: Agent Reach loaded, {len(self._channels)} channels")
        except ImportError:
            log.info("Hermes: Agent Reach not installed — built-in routing, Jina reader")
        except Exception as e:
            log.warning(f"Hermes: Agent Reach failed to load ({e}) — falling back")

    @property
    def has_agent_reach(self) -> bool:
        return bool(self._channels)

    # -- Routing ------------------------------------------------------------

    def identify(self, url: str) -> str:
        """Name the platform a URL belongs to. Falls back to 'web'."""
        url = normalize_url(url)

        for channel in self._channels:
            name = getattr(channel, "name", "")
            if name == WEB:
                continue  # can_handle() is True for everything — only ever last
            try:
                if channel.can_handle(url):
                    return name
            except Exception:
                continue  # one broken channel must not break routing

        host = _host_of(url)
        if host:
            for name, domains in _FALLBACK_HOSTS.items():
                if _domain_matches(host, *domains):
                    return name
        return WEB

    def _channel(self, name: str):
        for channel in self._channels:
            if getattr(channel, "name", "") == name:
                return channel
        return None

    # -- Availability -------------------------------------------------------

    async def availability(self, force: bool = False) -> dict[str, ChannelStatus]:
        """Which platforms are actually live here, cached for AVAILABILITY_TTL.

        Channel checks run real subprocess probes, so this is deliberately not
        on the per-request path.
        """
        fresh = (time.time() - self._availability_at) < AVAILABILITY_TTL
        if self._availability and fresh and not force:
            return self._availability

        if not self._channels:
            self._availability = {
                WEB: ChannelStatus(WEB, "ok", "Jina Reader — any URL, no setup", "Jina Reader")
            }
            self._availability_at = time.time()
            return self._availability

        async def probe(channel) -> ChannelStatus | None:
            name = getattr(channel, "name", "") or "?"
            try:
                status, message = await asyncio.to_thread(channel.check, self._config)
                return ChannelStatus(
                    name=name,
                    status=str(status),
                    message=str(message),
                    backend=getattr(channel, "active_backend", None) or "",
                    tier=getattr(channel, "tier", 0),
                )
            except Exception as e:
                log.debug(f"Hermes: probe failed for {name}: {e}")
                return ChannelStatus(name, "error", str(e))

        results = await asyncio.gather(
            *(probe(c) for c in self._channels), return_exceptions=True
        )
        self._availability = {
            r.name: r for r in results if isinstance(r, ChannelStatus)
        }
        self._availability_at = time.time()
        return self._availability

    async def status_report(self) -> str:
        """Human-readable reach report, for logs and the settings screen.

        Three states, because upstream's 'warn' is a real one: the platform's
        own tool is missing, but the URL is still readable through Jina. Calling
        that plain 'ok' next to a "not installed" message reads as a lie.
        """
        statuses = await self.availability()
        live = [s for s in statuses.values() if s.live]
        degraded = [s for s in statuses.values() if s.available and not s.live]

        lines = [
            f"Reach: {len(live)} live, {len(degraded)} via fallback, "
            f"{len(statuses) - len(live) - len(degraded)} unavailable"
        ]
        for s in sorted(statuses.values(), key=lambda s: (not s.live, not s.available, s.name)):
            mark = "live" if s.live else ("fallback" if s.available else "off")
            detail = f" ({s.backend})" if s.backend else ""
            first_line = s.message.splitlines()[0] if s.message else ""
            lines.append(f"  [{mark:>8}] {s.name}{detail}: {first_line}")
        return "\n".join(lines)

    # -- Reading ------------------------------------------------------------

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=self.timeout,
                follow_redirects=True,
                headers={"User-Agent": USER_AGENT},
            )
        return self._client

    def _trim(self, text: str) -> tuple[str, bool]:
        """Cap page text so a long article can't swamp the model's context."""
        if len(text) <= self.max_chars:
            return text, False
        cut = text[: self.max_chars]
        edge = cut.rfind(" ")
        if edge > self.max_chars * 0.8:  # only honour a word boundary if it's near the end
            cut = cut[:edge]
        return cut.rstrip() + "\n\n[... truncated]", True

    async def _read_jina(self, url: str) -> tuple[str, str]:
        """Read any URL as markdown via Jina Reader. Returns (title, text)."""
        client = await self._http()
        resp = await client.get(JINA_ENDPOINT + url, headers={"Accept": "text/plain"})
        resp.raise_for_status()
        body = resp.text

        # Jina prefixes a short header block; lift the title out of it.
        title = ""
        for line in body.splitlines()[:5]:
            if line.startswith("Title:"):
                title = line[len("Title:"):].strip()
                break
        return title, body

    async def _read_native(self, channel, url: str) -> tuple[str, str] | None:
        """Use a channel's own read() when it has one and its backend is live.

        Upstream channels are blocking, so this goes to a worker thread. Any
        failure returns None and the caller falls through to Jina.
        """
        reader = getattr(channel, "read", None)
        if not callable(reader):
            return None

        # Require a genuinely live backend. 'warn' means the platform's own tool
        # is missing and upstream cleared active_backend — reading natively then
        # would just fail slowly on the way to Jina.
        name = getattr(channel, "name", "")
        statuses = await self.availability()
        status = statuses.get(name)
        if status is not None and not status.live:
            return None

        try:
            text = await asyncio.wait_for(
                asyncio.to_thread(reader, url), timeout=self.timeout
            )
        except Exception as e:
            log.info(f"Hermes: native read failed for {name} ({e}) — falling back to Jina")
            return None

        return ("", text) if isinstance(text, str) and text.strip() else None

    async def read(self, url: str) -> ReachResult:
        """Fetch a URL and return its readable text. Never raises."""
        url = normalize_url(url)
        if not _host_of(url):
            return ReachResult(url=url, ok=False, error="not a valid http(s) URL")

        platform = self.identify(url)
        result = ReachResult(url=url, platform=platform)

        try:
            title, text, backend = "", "", ""

            channel = self._channel(platform)
            if channel is not None and platform != WEB:
                native = await self._read_native(channel, url)
                if native is not None:
                    title, text = native
                    backend = getattr(channel, "active_backend", "") or platform

            if not text:
                title, text = await self._read_jina(url)
                backend = "Jina Reader"

            text, truncated = self._trim(text)
            result.title = title
            result.text = text
            result.backend = backend
            result.truncated = truncated
            result.word_count = len(text.split())
            log.info(f"Hermes read {platform}: {result.summary()}")
            return result

        except httpx.TimeoutException:
            result.ok = False
            result.error = f"timed out after {self.timeout:.0f}s"
        except httpx.HTTPStatusError as e:
            result.ok = False
            result.error = f"HTTP {e.response.status_code}"
        except Exception as e:
            result.ok = False
            result.error = str(e)

        log.warning(f"Hermes: {result.summary()}")
        return result

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None


# Module-level instance, mirroring how the server holds its browser.
hermes = Hermes()
