"""
Tests for AlertMonitor — response parsing, area matching, deduplication.
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from alert_monitor import AlertMonitor, fetch_known_areas, validate_configured_areas


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
        m = make_monitor(areas=["חיפה"])
        payload = json.dumps({"id": "1", "cat": "1", "data": ["חיפה-כרמל"]})
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


# ---------------------------------------------------------------------------
# fetch_known_areas
# ---------------------------------------------------------------------------

def make_cities_session_mock(payload: str, raise_exc=None):
    """Return a session mock for the cities endpoint."""
    resp = AsyncMock()
    resp.raise_for_status = MagicMock(side_effect=raise_exc)
    resp.text = AsyncMock(return_value=payload)
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)

    session = MagicMock()
    session.get = MagicMock(return_value=resp)
    return session


_CITIES_RESPONSE = json.dumps([
    {"label": "תל אביב - מרכז", "value": "tel-aviv-center"},
    {"label": "תל אביב - דרום", "value": "tel-aviv-south"},
    {"label": "חיפה", "value": "haifa"},
    {"label": "באר שבע", "value": "beer-sheva"},
])


class TestFetchKnownAreas:
    @pytest.mark.asyncio
    async def test_returns_label_list(self):
        session = make_cities_session_mock(_CITIES_RESPONSE)
        result = await fetch_known_areas(session)
        assert "תל אביב - מרכז" in result
        assert "חיפה" in result
        assert len(result) == 4

    @pytest.mark.asyncio
    async def test_empty_list_response(self):
        session = make_cities_session_mock("[]")
        result = await fetch_known_areas(session)
        assert result == []

    @pytest.mark.asyncio
    async def test_non_list_json_returns_empty(self):
        session = make_cities_session_mock('{"error": "bad"}')
        result = await fetch_known_areas(session)
        assert result == []

    @pytest.mark.asyncio
    async def test_network_error_returns_empty(self):
        resp = AsyncMock()
        resp.raise_for_status = MagicMock(side_effect=Exception("timeout"))
        resp.__aenter__ = AsyncMock(return_value=resp)
        resp.__aexit__ = AsyncMock(return_value=False)
        session = MagicMock()
        session.get = MagicMock(return_value=resp)
        result = await fetch_known_areas(session)
        assert result == []

    @pytest.mark.asyncio
    async def test_items_without_label_are_skipped(self):
        payload = json.dumps([
            {"label": "תל אביב", "value": "ta"},
            {"value": "no-label"},           # missing label
            {"label": "חיפה", "value": "h"},
        ])
        session = make_cities_session_mock(payload)
        result = await fetch_known_areas(session)
        assert result == ["תל אביב", "חיפה"]


# ---------------------------------------------------------------------------
# validate_configured_areas
# ---------------------------------------------------------------------------

_KNOWN = ["תל אביב - מרכז", "תל אביב - דרום", "חיפה", "באר שבע", "ירושלים"]


class TestValidateConfiguredAreas:
    def test_all_valid_returns_empty(self):
        assert validate_configured_areas(["תל אביב", "חיפה"], _KNOWN) == []

    def test_exact_match_is_valid(self):
        assert validate_configured_areas(["חיפה"], _KNOWN) == []

    def test_substring_match_is_valid(self):
        # "תל אביב" is a substring of "תל אביב - מרכז"
        assert validate_configured_areas(["תל אביב"], _KNOWN) == []

    def test_typo_is_flagged(self):
        bad = validate_configured_areas(["תל אבייב"], _KNOWN)  # extra yod
        assert "תל אבייב" in bad

    def test_completely_unknown_area_flagged(self):
        bad = validate_configured_areas(["eilat"], _KNOWN)
        assert "eilat" in bad

    def test_case_insensitive(self):
        assert validate_configured_areas(["TEL AVIV"], ["Tel Aviv - Center"]) == []

    def test_mixed_valid_and_invalid(self):
        bad = validate_configured_areas(["חיפה", "tzfat"], _KNOWN)
        assert bad == ["tzfat"]

    def test_empty_configured_returns_empty(self):
        assert validate_configured_areas([], _KNOWN) == []

    def test_empty_known_flags_everything(self):
        # If the city list couldn't be fetched, known=[] → don't call this
        # (callers skip validation), but the function itself flags all areas.
        bad = validate_configured_areas(["חיפה"], [])
        assert "חיפה" in bad
