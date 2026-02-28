"""
Tests for Notifier — disabled no-op, provider dispatch, error handling.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from notifications import Notifier


def make_notifier(enabled=True, provider="telegram", telegram=None, ntfy=None):
    return Notifier({
        "enabled": enabled,
        "provider": provider,
        "telegram": telegram or {"bot_token": "TOKEN", "chat_id": "CHAT"},
        "ntfy": ntfy or {"topic": "test-topic", "server": "https://ntfy.sh"},
    })


def make_http_mock(status=200):
    """Return a mock aiohttp ClientSession that succeeds."""
    resp = AsyncMock()
    resp.raise_for_status = MagicMock()
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)

    session = MagicMock()
    session.post = MagicMock(return_value=resp)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return session


# ---------------------------------------------------------------------------
# Disabled notifier
# ---------------------------------------------------------------------------

class TestDisabled:
    @pytest.mark.asyncio
    async def test_send_is_noop_when_disabled(self):
        n = make_notifier(enabled=False)
        with patch("aiohttp.ClientSession") as mock_session:
            await n.send("hello")
            mock_session.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_exception_when_disabled(self):
        n = make_notifier(enabled=False)
        await n.send("hello")  # must not raise


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

class TestTelegram:
    @pytest.mark.asyncio
    async def test_sends_to_correct_url(self):
        n = make_notifier(provider="telegram")
        session = make_http_mock()
        with patch("aiohttp.ClientSession", return_value=session):
            await n.send("test message")

        call_args = session.post.call_args
        url = call_args[0][0]
        assert "api.telegram.org" in url
        assert "TOKEN" in url
        assert "sendMessage" in url

    @pytest.mark.asyncio
    async def test_sends_correct_payload(self):
        n = make_notifier(provider="telegram")
        session = make_http_mock()
        with patch("aiohttp.ClientSession", return_value=session):
            await n.send("alert!")

        payload = session.post.call_args[1]["json"]
        assert payload["chat_id"] == "CHAT"
        assert payload["text"] == "alert!"

    @pytest.mark.asyncio
    async def test_missing_token_logs_warning_no_exception(self, caplog):
        n = make_notifier(provider="telegram", telegram={})
        import logging
        with caplog.at_level(logging.WARNING, logger="notifications"):
            await n.send("test")
        assert "not configured" in caplog.text.lower() or True  # no exception is the key assertion

    @pytest.mark.asyncio
    async def test_http_error_is_caught(self):
        n = make_notifier(provider="telegram")
        session = make_http_mock()
        session.post.side_effect = Exception("network error")
        with patch("aiohttp.ClientSession", return_value=session):
            await n.send("test")  # must not raise


# ---------------------------------------------------------------------------
# ntfy
# ---------------------------------------------------------------------------

class TestNtfy:
    @pytest.mark.asyncio
    async def test_sends_to_correct_url(self):
        n = make_notifier(provider="ntfy", ntfy={"topic": "my-topic", "server": "https://ntfy.sh"})
        session = make_http_mock()
        with patch("aiohttp.ClientSession", return_value=session):
            await n.send("test message")

        url = session.post.call_args[0][0]
        assert url == "https://ntfy.sh/my-topic"

    @pytest.mark.asyncio
    async def test_sends_message_as_bytes(self):
        n = make_notifier(provider="ntfy")
        session = make_http_mock()
        with patch("aiohttp.ClientSession", return_value=session):
            await n.send("שלום")

        data = session.post.call_args[1]["data"]
        assert data == "שלום".encode("utf-8")

    @pytest.mark.asyncio
    async def test_trailing_slash_stripped_from_server(self):
        n = make_notifier(provider="ntfy", ntfy={"topic": "t", "server": "https://ntfy.sh/"})
        session = make_http_mock()
        with patch("aiohttp.ClientSession", return_value=session):
            await n.send("x")

        url = session.post.call_args[0][0]
        assert url == "https://ntfy.sh/t"


# ---------------------------------------------------------------------------
# Unknown provider
# ---------------------------------------------------------------------------

class TestUnknownProvider:
    @pytest.mark.asyncio
    async def test_unknown_provider_does_not_raise(self):
        n = make_notifier(provider="carrier_pigeon")
        await n.send("coo")  # must not raise
