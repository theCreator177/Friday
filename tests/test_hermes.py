"""
Tests for the Hermes reach layer.

Everything here runs offline. The one network-dependent test is skipped when
there's no connectivity, matching test_browser_integration.py.

Run: python3 -m pytest tests/test_hermes.py -v
"""

import socket
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from hermes import (
    WEB,
    ChannelStatus,
    Hermes,
    ReachResult,
    _domain_matches,
    _host_of,
    normalize_url,
)


def _has_network() -> bool:
    try:
        socket.create_connection(("1.1.1.1", 53), timeout=3)
        return True
    except OSError:
        return False


NETWORK_AVAILABLE = _has_network()


@pytest.fixture
def h():
    return Hermes()


# ── URL normalisation ─────────────────────────────────────────────────


def test_normalize_adds_scheme():
    assert normalize_url("github.com/torvalds") == "https://github.com/torvalds"


def test_normalize_leaves_scheme_alone():
    assert normalize_url("http://example.com") == "http://example.com"
    assert normalize_url("https://example.com") == "https://example.com"


def test_normalize_handles_empty():
    assert normalize_url("") == ""
    assert normalize_url("   ") == ""


# ── Host matching is a security boundary, not a convenience ───────────


def test_domain_matches_exact_and_subdomain():
    assert _domain_matches("github.com", "github.com")
    assert _domain_matches("gist.github.com", "github.com")


def test_domain_rejects_lookalike_suffix():
    """The bug a substring match would introduce."""
    assert not _domain_matches("github.com.evil.test", "github.com")
    assert not _domain_matches("notgithub.com", "github.com")


def test_host_of_rejects_userinfo_disguise():
    """https://github.com@evil.test must resolve to evil.test, not github.com."""
    assert _host_of("https://github.com@evil.test/path") == ""


def test_host_of_rejects_non_http_schemes():
    assert _host_of("file:///etc/passwd") == ""
    assert _host_of("javascript:alert(1)") == ""
    assert _host_of("ftp://example.com") == ""


def test_host_of_accepts_normal_urls():
    assert _host_of("https://www.youtube.com/watch?v=abc") == "www.youtube.com"


def test_host_of_survives_malformed_port():
    assert _host_of("https://example.com:99999999/") == ""


# ── Platform routing ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://github.com/torvalds/linux", "github"),
        ("https://x.com/user/status/1", "twitter"),
        ("https://twitter.com/user", "twitter"),
        ("https://www.youtube.com/watch?v=abc", "youtube"),
        ("https://youtu.be/abc", "youtube"),
        ("https://reddit.com/r/python", "reddit"),
        ("https://www.linkedin.com/in/someone", "linkedin"),
        ("https://example.com/article", WEB),
        ("https://news.ycombinator.com/item?id=1", WEB),
    ],
)
def test_identify_routes_known_platforms(h, url, expected):
    assert h.identify(url) == expected


def test_identify_accepts_bare_host(h):
    assert h.identify("github.com/torvalds") == "github"


def test_identify_falls_back_to_web_for_lookalikes(h):
    """A lookalike must not be routed to the platform it imitates."""
    assert h.identify("https://github.com.evil.test/repo") == WEB


def test_identify_never_returns_empty(h):
    assert h.identify("not a url at all") == WEB


# ── Truncation ────────────────────────────────────────────────────────


def test_trim_leaves_short_text_alone():
    h = Hermes(max_chars=100)
    text, truncated = h._trim("short")
    assert text == "short"
    assert truncated is False


def test_trim_caps_long_text():
    h = Hermes(max_chars=100)
    text, truncated = h._trim("word " * 500)
    assert truncated is True
    assert "[... truncated]" in text
    assert len(text) < 200


def test_trim_handles_text_with_no_spaces():
    """A word boundary must never be required to exist."""
    h = Hermes(max_chars=50)
    text, truncated = h._trim("x" * 500)
    assert truncated is True
    assert text.startswith("x" * 50)


# ── Result model ──────────────────────────────────────────────────────


def test_result_summary_reports_failure():
    r = ReachResult(url="https://x.test", ok=False, error="timed out")
    assert "Could not reach" in r.summary()
    assert "timed out" in r.summary()


def test_result_summary_prefers_title():
    r = ReachResult(url="https://x.test", title="A Title", word_count=10, backend="Jina Reader")
    assert "A Title" in r.summary()
    assert "Jina Reader" in r.summary()


def test_result_to_dict_is_json_shaped():
    r = ReachResult(url="https://x.test")
    d = r.to_dict()
    assert d["url"] == "https://x.test"
    assert d["ok"] is True


def test_channel_status_availability():
    assert ChannelStatus("web", "ok").available
    assert ChannelStatus("web", "warn").available
    assert not ChannelStatus("yt", "off").available
    assert not ChannelStatus("yt", "error").available


def test_channel_status_live_requires_a_real_backend():
    """Upstream returns warn + no backend for 'tool missing, fallback works'.

    That is reachable but not live, and the two must not be conflated.
    """
    assert ChannelStatus("web", "ok", backend="Jina Reader").live
    assert not ChannelStatus("github", "warn", backend="").live
    assert not ChannelStatus("github", "ok", backend="").live
    assert not ChannelStatus("yt", "off", backend="yt-dlp").live


# ── read() ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_read_rejects_invalid_url(h):
    r = await h.read("javascript:alert(1)")
    assert r.ok is False
    assert "valid http" in r.error
    await h.close()


@pytest.mark.asyncio
async def test_read_uses_jina_and_parses_title(h, monkeypatch):
    async def fake_jina(url):
        return "Example Domain", "Title: Example Domain\n\nSome body text here."

    monkeypatch.setattr(h, "_read_jina", fake_jina)
    r = await h.read("example.com")

    assert r.ok is True
    assert r.title == "Example Domain"
    assert r.backend == "Jina Reader"
    assert r.platform == WEB
    assert r.word_count > 0
    await h.close()


@pytest.mark.asyncio
async def test_read_never_raises_on_transport_error(h, monkeypatch):
    """A dead network must degrade to ok=False, never blow up the voice loop."""

    async def boom(url):
        raise RuntimeError("network is down")

    monkeypatch.setattr(h, "_read_jina", boom)
    r = await h.read("https://example.com")

    assert r.ok is False
    assert "network is down" in r.error
    await h.close()


@pytest.mark.asyncio
async def test_availability_without_agent_reach_still_offers_web():
    """Reach is never zero — Jina always backs the web channel."""
    h = Hermes()
    h._channels = []
    statuses = await h.availability(force=True)

    assert WEB in statuses
    assert statuses[WEB].available
    await h.close()


@pytest.mark.asyncio
async def test_availability_is_cached(h, monkeypatch):
    """Channel probes shell out — they must not run per request."""
    h._channels = []
    first = await h.availability(force=True)
    stamp = h._availability_at
    second = await h.availability()

    assert second is first
    assert h._availability_at == stamp
    await h.close()


@pytest.mark.asyncio
async def test_status_report_is_readable(h):
    h._channels = []
    report = await h.status_report()

    assert "Reach:" in report
    assert WEB in report
    await h.close()


@pytest.mark.asyncio
async def test_status_report_separates_live_from_fallback(h):
    """A channel whose own tool is missing must not be reported as live."""
    h._channels = []
    h._availability = {
        "web": ChannelStatus("web", "ok", "Jina Reader", "Jina Reader"),
        "github": ChannelStatus("github", "warn", "gh CLI not installed", ""),
        "youtube": ChannelStatus("youtube", "off", "yt-dlp not installed", ""),
    }
    h._availability_at = time.time()
    report = await h.status_report()

    assert "1 live, 1 via fallback, 1 unavailable" in report
    assert "[    live] web" in report
    assert "[fallback] github" in report
    assert "[     off] youtube" in report
    await h.close()


@pytest.mark.asyncio
async def test_native_read_skipped_when_backend_not_live(h):
    """Don't call a channel's read() when its backend is gone — go to Jina."""

    class Fake:
        name = "github"

        def read(self, url):
            raise AssertionError("native read must not be attempted")

    h._availability = {"github": ChannelStatus("github", "warn", "gh missing", "")}
    h._availability_at = time.time()

    assert await h._read_native(Fake(), "https://github.com/x") is None
    await h.close()


# ── Server wiring ─────────────────────────────────────────────────────


def _extract_action():
    """server.py pulls in the full app stack; skip if it isn't installed."""
    try:
        from server import extract_action
    except Exception as e:
        pytest.skip(f"server not importable: {e}")
    return extract_action


def test_reach_tag_is_extracted_and_stripped_from_speech():
    extract_action = _extract_action()
    clean, action = extract_action(
        "Right away, sir. [ACTION:REACH] https://github.com/torvalds/linux"
    )

    assert action == {"action": "reach", "target": "https://github.com/torvalds/linux"}
    assert "[ACTION:" not in clean  # the tag must never reach TTS
    assert clean == "Right away, sir."


def test_browse_tag_still_works():
    """REACH must not have shadowed the existing action."""
    extract_action = _extract_action()
    _, action = extract_action("Certainly. [ACTION:BROWSE] github.com")

    assert action["action"] == "browse"


# ── Live network ──────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.skipif(not NETWORK_AVAILABLE, reason="No network")
async def test_read_real_page_end_to_end(h):
    r = await h.read("https://example.com")

    if r.ok:
        assert r.word_count > 0
        assert r.backend == "Jina Reader"
    else:
        # Jina rate-limits anonymous reads; a refusal is not a code defect.
        assert r.error
    await h.close()
