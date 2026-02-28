"""
Tests for AlertMonitor — response parsing, area matching, deduplication.
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from alert_monitor import AlertMonitor


def make_monitor(areas=None, alert_types=None):
    return AlertMonitor(
        areas=areas or ["תל אביב"],
        poll_seconds=1,
        alert_types=alert_types or ["1"],
    )


def make_session_mock(text: str):
    """Return an aiohttp ClientSession mock that yields *text* on GET."""
    resp = AsyncMock()
    resp.raise_for_status = MagicMock()
    resp.text = AsyncMock(return_value=text)
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)

    session = MagicMock()
    session.get = MagicMock(return_value=resp)
    return session


# ---------------------------------------------------------------------------
# _poll() — response parsing
# ---------------------------------------------------------------------------

class TestPoll:
    @pytest.mark.asyncio
    async def test_empty_response_returns_none(self):
        m = make_monitor()
        session = make_session_mock("")
        result = await m._poll(session)
        assert result is None

    @pytest.mark.asyncio
    async def test_whitespace_only_returns_none(self):
        m = make_monitor()
        session = make_session_mock("   \n  ")
        result = await m._poll(session)
        assert result is None

    @pytest.mark.asyncio
    async def test_invalid_json_returns_none(self):
        m = make_monitor()
        session = make_session_mock("not json")
        result = await m._poll(session)
        assert result is None

    @pytest.mark.asyncio
    async def test_non_dict_json_returns_none(self):
        m = make_monitor()
        session = make_session_mock("[1, 2, 3]")
        result = await m._poll(session)
        assert result is None


# ---------------------------------------------------------------------------
# Area matching
# ---------------------------------------------------------------------------

class TestAreaMatching:
    @pytest.mark.asyncio
    async def test_matching_area_returns_alert(self):
        m = make_monitor(areas=["תל אביב"])
        payload = json.dumps({"id": "1", "cat": "1", "data": ["תל אביב - מרכז"]})
        session = make_session_mock(payload)
        result = await m._poll(session)
        assert result is not None
        assert result["id"] == "1"

    @pytest.mark.asyncio
    async def test_non_matching_area_returns_none(self):
        m = make_monitor(areas=["חיפה"])
        payload = json.dumps({"id": "1", "cat": "1", "data": ["תל אביב - מרכז"]})
        session = make_session_mock(payload)
        result = await m._poll(session)
        assert result is None

    @pytest.mark.asyncio
    async def test_area_match_is_case_insensitive(self):
        m = make_monitor(areas=["TEL AVIV"])
        payload = json.dumps({"id": "1", "cat": "1", "data": ["tel aviv center"]})
        session = make_session_mock(payload)
        result = await m._poll(session)
        assert result is not None

    @pytest.mark.asyncio
    async def test_partial_area_match(self):
        m = make_monitor(areas=["קדימה"])
        payload = json.dumps({"id": "1", "cat": "1", "data": ["קדימה-צורן"]})
        session = make_session_mock(payload)
        result = await m._poll(session)
        assert result is not None

    @pytest.mark.asyncio
    async def test_multiple_areas_any_match(self):
        m = make_monitor(areas=["חיפה", "תל אביב"])
        payload = json.dumps({"id": "1", "cat": "1", "data": ["תל אביב - דרום"]})
        session = make_session_mock(payload)
        result = await m._poll(session)
        assert result is not None


# ---------------------------------------------------------------------------
# Alert type filtering
# ---------------------------------------------------------------------------

class TestAlertTypes:
    @pytest.mark.asyncio
    async def test_wrong_category_returns_none(self):
        m = make_monitor(alert_types=["1"])
        payload = json.dumps({"id": "1", "cat": "2", "data": ["תל אביב"]})
        session = make_session_mock(payload)
        result = await m._poll(session)
        assert result is None

    @pytest.mark.asyncio
    async def test_correct_category_passes(self):
        m = make_monitor(alert_types=["1"])
        payload = json.dumps({"id": "1", "cat": "1", "data": ["תל אביב"]})
        session = make_session_mock(payload)
        result = await m._poll(session)
        assert result is not None


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

class TestDeduplication:
    @pytest.mark.asyncio
    async def test_duplicate_id_returns_none(self):
        m = make_monitor(areas=["תל אביב"])
        payload = json.dumps({"id": "42", "cat": "1", "data": ["תל אביב"]})
        session = make_session_mock(payload)

        first = await m._poll(session)
        assert first is not None

        session2 = make_session_mock(payload)
        second = await m._poll(session2)
        assert second is None

    @pytest.mark.asyncio
    async def test_new_id_after_duplicate_passes(self):
        m = make_monitor(areas=["תל אביב"])
        p1 = json.dumps({"id": "1", "cat": "1", "data": ["תל אביב"]})
        p2 = json.dumps({"id": "2", "cat": "1", "data": ["תל אביב"]})

        await m._poll(make_session_mock(p1))
        result = await m._poll(make_session_mock(p2))
        assert result is not None
        assert result["id"] == "2"
