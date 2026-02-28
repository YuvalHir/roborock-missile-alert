"""
Tests for MamadService.on_alert — orchestration logic with mocked dependencies.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mamad_roborock import MamadService
from vacuum_controller import STATUS_OK, STATUS_LOW_BATTERY, STATUS_ALREADY_CLEANING, STATUS_ERROR


ALERT = {"id": "99", "cat": "1", "data": ["תל אביב"]}
ROOMS = [{"id": 1, "name": "Kitchen"}, {"id": 2, "name": "Living Room"}]


def make_service():
    cfg = {
        "areas": ["תל אביב"],
        "poll_seconds": 5,
        "alert_types": ["1"],
        "clean_duration_minutes": 0.01,  # very short for tests
        "fan_speed": "balanced",
        "cooldown_hours": 1,
        "min_battery_percent": 20,
        "state_file": "/tmp/test_mamad_state.json",
        "notifications": {"enabled": False},
    }
    service = MamadService(cfg)
    service.rooms = list(ROOMS)

    # Mock sub-components
    service.scheduler = MagicMock()
    service.scheduler.get_next_room = MagicMock(return_value=ROOMS[0])
    service.scheduler.mark_cleaned = MagicMock()
    service.scheduler.save = MagicMock()

    service.vacuum = MagicMock()
    service.vacuum.get_status = AsyncMock(return_value={
        "state": "idle", "battery": 80, "error_code": 0, "result": STATUS_OK
    })
    service.vacuum.start_segment_clean = AsyncMock()
    service.vacuum.stop_and_dock = AsyncMock()

    service.notifier = MagicMock()
    service.notifier.send = AsyncMock()

    return service


# ---------------------------------------------------------------------------
# on_alert — guard conditions
# ---------------------------------------------------------------------------

class TestOnAlertGuards:
    @pytest.mark.asyncio
    async def test_already_cleaning_skips(self):
        s = make_service()
        s.is_cleaning = True
        await s.on_alert(ALERT)
        s.vacuum.start_segment_clean.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_eligible_rooms_skips(self):
        s = make_service()
        s.scheduler.get_next_room = MagicMock(return_value=None)
        await s.on_alert(ALERT)
        s.vacuum.start_segment_clean.assert_not_called()

    @pytest.mark.asyncio
    async def test_low_battery_skips(self):
        s = make_service()
        s.vacuum.get_status = AsyncMock(return_value={
            "state": "idle", "battery": 10, "error_code": 0, "result": STATUS_LOW_BATTERY
        })
        await s.on_alert(ALERT)
        s.vacuum.start_segment_clean.assert_not_called()

    @pytest.mark.asyncio
    async def test_vacuum_error_skips(self):
        s = make_service()
        s.vacuum.get_status = AsyncMock(return_value={
            "state": "error", "battery": 80, "error_code": 5, "result": STATUS_ERROR
        })
        await s.on_alert(ALERT)
        s.vacuum.start_segment_clean.assert_not_called()

    @pytest.mark.asyncio
    async def test_already_cleaning_state_skips(self):
        s = make_service()
        s.vacuum.get_status = AsyncMock(return_value={
            "state": "segment_cleaning", "battery": 80, "error_code": 0,
            "result": STATUS_ALREADY_CLEANING
        })
        await s.on_alert(ALERT)
        s.vacuum.start_segment_clean.assert_not_called()


# ---------------------------------------------------------------------------
# on_alert — happy path
# ---------------------------------------------------------------------------

class TestOnAlertHappyPath:
    @pytest.mark.asyncio
    async def test_starts_cleaning_correct_room(self):
        s = make_service()
        await s.on_alert(ALERT)
        # Give the task a moment to start
        await asyncio.sleep(0.05)
        s.vacuum.start_segment_clean.assert_called_once_with(ROOMS[0]["id"], fan_speed="balanced")

    @pytest.mark.asyncio
    async def test_sets_is_cleaning_flag(self):
        s = make_service()
        await s.on_alert(ALERT)
        assert s.is_cleaning is True
        # Wait for task to complete
        if s.cleaning_task:
            await s.cleaning_task

    @pytest.mark.asyncio
    async def test_clears_is_cleaning_after_done(self):
        s = make_service()
        await s.on_alert(ALERT)
        if s.cleaning_task:
            await s.cleaning_task
        assert s.is_cleaning is False

    @pytest.mark.asyncio
    async def test_marks_room_cleaned_after_session(self):
        s = make_service()
        await s.on_alert(ALERT)
        if s.cleaning_task:
            await s.cleaning_task
        s.scheduler.mark_cleaned.assert_called_once_with(ROOMS[0]["id"])

    @pytest.mark.asyncio
    async def test_docks_after_duration(self):
        s = make_service()
        await s.on_alert(ALERT)
        if s.cleaning_task:
            await s.cleaning_task
        s.vacuum.stop_and_dock.assert_called_once()

    @pytest.mark.asyncio
    async def test_second_alert_ignored_while_cleaning(self):
        s = make_service()
        await s.on_alert(ALERT)
        assert s.is_cleaning is True
        call_count_before = s.vacuum.start_segment_clean.call_count
        await s.on_alert(ALERT)
        assert s.vacuum.start_segment_clean.call_count == call_count_before
