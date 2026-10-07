"""Static checks on app/static/overlay.html (served under the /tts/ rewrite)."""
import re
from pathlib import Path

OVERLAY = Path(__file__).resolve().parent.parent / "app" / "static" / "overlay.html"


def _html() -> str:
    return OVERLAY.read_text(encoding="utf-8")


def test_overlay_exists():
    assert OVERLAY.is_file()


def test_no_absolute_urls():
    html = _html()
    for needle in ("http://", "https://", "'/ws'", '"/ws"', "/overlay"):
        assert needle not in html, needle
    assert not re.search(r"""['"]/[A-Za-z]""", html), "absolute path literal found"


def test_no_external_resources():
    html = _html()
    assert not re.search(r"<script[^>]*\bsrc\s*=", html, re.I)
    assert not re.search(r"<link[^>]*\bhref\s*=", html, re.I)
    assert "@import" not in html


def test_caption_and_relative_ws_url():
    html = _html()
    assert "TTS" in html
    assert "new URL('ws'" in html
    assert "location.search" in html
    assert "location.href" in html
    assert "wss:" in html


def test_caption_uses_textcontent_not_innerhtml():
    html = _html()
    assert "textContent" in html
    assert not re.search(r"innerHTML|outerHTML|insertAdjacentHTML|document\.write", html)


def test_protocol_messages_handled():
    html = _html()
    for token in ("'played'", "'skip'", "'clear'", "'paused'", "'ping'", "'pong'", "debug"):
        assert token in html, token


def test_transparent_background():
    assert re.search(r"background:\s*transparent", _html())


def test_caption_honours_server_duration():
    """The caption must stay for duration_s even if the media element ends early (OBS), and must be
    force-ended shortly after that length when `ended` never fires."""
    src = OVERLAY.read_text(encoding="utf-8")
    assert "msg.duration_s" in src
    assert "GRACE_MS" in src
    assert "onMediaDone" in src and "onPlaybackStarted" in src
    # skip/clear still end the item immediately, bypassing the duration clock
    assert "case 'skip':\n        finishCurrent();" in src
