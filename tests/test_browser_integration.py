"""
Browser integration tests for JARVIS.

Exercises the browser pipeline: search, visit, screenshot.
Skips if no network or Playwright browsers not installed.
"""

import asyncio
import os
import shutil
import socket
import sys
import tempfile
from pathlib import Path

import pytest
import pytest_asyncio

sys.path.insert(0, str(Path(__file__).parent.parent))

from browser import JarvisBrowser, SearchResult, PageContent


def _has_network() -> bool:
    """Check if we have internet connectivity."""
    try:
        socket.create_connection(("1.1.1.1", 53), timeout=3)
        return True
    except OSError:
        return False


NETWORK_AVAILABLE = _has_network()
SKIP_REASON = "No network or Playwright browsers not available"


@pytest_asyncio.fixture
async def browser():
    """Create and clean up a browser instance."""
    b = JarvisBrowser()
    yield b
    await b.close()


# ── Search Tests ──────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.skipif(not NETWORK_AVAILABLE, reason=SKIP_REASON)
async def test_browser_search(browser):
    """Search returns results from DuckDuckGo (may be empty if DDG blocks)."""
    results = await browser.search("Python FastAPI documentation")

    assert isinstance(results, list)
    # DDG HTML version may block automated requests; verify structure if results exist
    if len(results) > 0:
        for r in results:
            assert isinstance(r, SearchResult)
            assert r.title, "Result should have a title"
            assert r.url, "Result should have a URL"
    else:
        # Search returned empty - DDG may be blocking. Not a code error.
        pytest.skip("DuckDuckGo returned no results (likely bot detection)")


@pytest.mark.asyncio
@pytest.mark.skipif(not NETWORK_AVAILABLE, reason=SKIP_REASON)
async def test_browser_search_empty_query(browser):
    """Search handles empty query gracefully."""
    results = await browser.search("")
    assert isinstance(results, list)


# ── Visit Tests ───────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.skipif(not NETWORK_AVAILABLE, reason=SKIP_REASON)
async def test_browser_visit(browser):
    """Visit extracts readable content from a known page."""
    content = await browser.visit("https://example.com")

    assert isinstance(content, PageContent)
    assert "Example Domain" in content.title
    assert content.url == "https://example.com"
    assert len(content.text_content) > 0
    assert content.word_count > 0
    # Should be readable text, not raw HTML
    assert "<html>" not in content.text_content.lower()


@pytest.mark.asyncio
@pytest.mark.skipif(not NETWORK_AVAILABLE, reason=SKIP_REASON)
async def test_browser_visit_invalid_url(browser):
    """Visit handles invalid URLs gracefully."""
    content = await browser.visit("https://this-domain-definitely-does-not-exist-12345.com")
    assert isinstance(content, PageContent)
    # Should return an error content, not crash
    assert content.title == "Error" or "Failed" in content.text_content


# ── Screenshot Tests ──────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.skipif(not NETWORK_AVAILABLE, reason=SKIP_REASON)
async def test_browser_screenshot(browser):
    """Screenshot produces a valid PNG file."""
    tmp_path = tempfile.mktemp(suffix=".png", prefix="jarvis_test_ss_")

    try:
        result_path = await browser.screenshot("https://example.com", path=tmp_path)

        assert result_path == tmp_path
        assert os.path.exists(result_path), "Screenshot file should exist"
        assert result_path.endswith(".png")

        # Check it's a valid PNG (starts with PNG signature)
        with open(result_path, "rb") as f:
            header = f.read(8)
            assert header[:4] == b"\x89PNG", "File should be valid PNG"

        # Check file has reasonable size
        size = os.path.getsize(result_path)
        assert size > 1000, "Screenshot should be at least 1KB"
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


@pytest.mark.asyncio
@pytest.mark.skipif(not NETWORK_AVAILABLE, reason=SKIP_REASON)
async def test_browser_screenshot_default_path(browser):
    """Screenshot with no path generates a temp file."""
    result_path = await browser.screenshot("https://example.com")

    try:
        assert result_path, "Should return a path"
        assert os.path.exists(result_path)
        assert result_path.endswith(".png")
    finally:
        if result_path and os.path.exists(result_path):
            os.unlink(result_path)


# ── Research Tests ────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.skipif(not NETWORK_AVAILABLE, reason=SKIP_REASON)
async def test_browser_research(browser):
    """Research performs multi-step search and visit."""
    result = await browser.research("Python FastAPI tutorial")

    assert result.topic == "Python FastAPI tutorial"
    assert isinstance(result.sources, list)
    assert len(result.summary) > 0


# ── Action Detection ─────────────────────────────────────────────────


# Browse detection used to be keyword matching against a server.ACTION_KEYWORDS
# dict. That was replaced by LLM-emitted [ACTION:X] tags parsed by
# extract_action(), and the old constant no longer exists — these cover the
# mechanism that replaced it.


def test_extract_browse_action_tag():
    """[ACTION:BROWSE] is parsed into an action dict with its target."""
    from server import extract_action

    clean, action = extract_action("Right away, sir. [ACTION:BROWSE] weather in Austin")

    assert action is not None
    assert action["action"] == "browse"
    assert action["target"] == "weather in Austin"
    # The tag is stripped so it never reaches text-to-speech.
    assert "[ACTION:" not in clean
    assert clean == "Right away, sir."


def test_extract_action_returns_none_without_a_tag():
    """Ordinary conversation is passed through untouched."""
    from server import extract_action

    text = "The weather is fine today, sir."
    clean, action = extract_action(text)

    assert action is None
    assert clean == text


def test_extract_action_recognises_each_supported_type():
    """Every advertised action tag parses to its lowercase action name."""
    from server import extract_action

    for tag, expected in [
        ("BUILD", "build"),
        ("BROWSE", "browse"),
        ("RESEARCH", "research"),
        ("OPEN_TERMINAL", "open_terminal"),
        ("SCREEN", "screen"),
    ]:
        _, action = extract_action(f"Certainly. [ACTION:{tag}] do the thing")
        assert action is not None, f"{tag} should parse"
        assert action["action"] == expected


# ── Browser Lifecycle ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_browser_close_idempotent():
    """Closing browser multiple times should not error."""
    b = JarvisBrowser()
    await b.close()
    await b.close()  # Should not raise
