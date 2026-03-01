"""
End-to-end flow test: mocked alert API → AlertMonitor polling → MamadService.on_alert → vacuum.

These tests wire together the real AlertMonitor and MamadService (with mocked HTTP
and vacuum/scheduler/notifier) to verify the full chain fires correctly, including
the negative cases where cleaning must NOT start.
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from alert_monitor import AlertMonitor
from mamad_roborock import MamadService
from vacuum_controller import STATUS_OK

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

# A real-looking alert for תל אביב (category 1 = missiles)
_TEL_AVIV_ALERT = json.dumps({
    "id": "12345",
    "cat": "1",
    "title": "ירי רקטות וטילים",
    "data": ["תל אביב - מרכז", "תל אביב - דרום"],
    "desc": "היכנסו למרחב המוגן",
})

_ROOMS = [{"id": 16, "name": "Kitchen"}, {"id": 17, "name": "Living Room"}]

_POLL_SECONDS = 1  # AlertMonitor poll interval for all e2e tests


def _make_http_mock(text: str):
    """
    Build an aiohttp.ClientSession mock whose GET responses always return *text*.

    Handles both context-manager layers used by AlertMonitor:
      async with aiohttp.ClientSession() as session:          <- outer
          async with session.get(url, ...) as resp:           <- inner
    """
    resp = AsyncMock()
    resp.raise_for_status = MagicMock()
    resp.read = AsyncMock(return_value=text.encode("utf-8"))
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)

    session = MagicMock()
    session.get = MagicMock(return_value=resp)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return session


def _make_service(cfg_overrides=None):
    """Return a MamadService with all external dependencies mocked out."""
    cfg = {
        "areas": ["תל אביב"],
        "poll_seconds": _POLL_SECONDS,
        "alert_types": ["1"],
        "clean_duration_minutes": 0.01,   # ~0.6 s so tests finish quickly
        "fan_speed": "balanced",
        "cooldown_hours": 1,
        "min_battery_percent": 20,
        "state_file": "/tmp/test_e2e_state.json",
        "notifications": {"enabled": False},
    }
    if cfg_overrides:
        cfg.update(cfg_overrides)

    service = MamadService(cfg)
    service.rooms = list(_ROOMS)

    service.scheduler = MagicMock()
    service.scheduler.get_next_room = MagicMock(return_value=_ROOMS[0])
    service.scheduler.mark_cleaned = MagicMock()
    service.scheduler.save = MagicMock()

    service.vacuum = MagicMock()
    service.vacuum.get_status = AsyncMock(return_value={
        "state": "idle", "battery": 85, "error_code": 0, "result": STATUS_OK
    })
    service.vacuum.start_segment_clean = AsyncMock()
    service.vacuum.stop_and_dock = AsyncMock()

    service.notifier = MagicMock()
    service.notifier.send = AsyncMock()

    return service


async def _run_monitor_until(monitor: AlertMonitor, callback, stop_condition, timeout=3.0):
    """
    Start *monitor* as a background task.

    Polls *stop_condition()* every 50 ms until it returns True or *timeout* elapses,
    then stops the monitor gracefully and returns.
    """
    task = asyncio.create_task(monitor.start(callback))
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if stop_condition():
            break
        await asyncio.sleep(0.05)
    monitor.stop()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


# ---------------------------------------------------------------------------
# Happy-path: alert fires and triggers cleaning
# ---------------------------------------------------------------------------

class TestE2EHappyPath:
    @pytest.mark.asyncio
    async def test_matching_alert_triggers_vacuum_clean(self):
        """Matching alert from API → AlertMonitor fires → vacuum.start_segment_clean called."""
        service = _make_service()
        monitor = AlertMonitor(areas=["תל אביב"], poll_seconds=_POLL_SECONDS, alert_types=["1"])
        http_mock = _make_http_mock(_TEL_AVIV_ALERT)

        with patch("aiohttp.ClientSession", return_value=http_mock):
            await _run_monitor_until(
                monitor,
                service.on_alert,
                stop_condition=lambda: service.vacuum.start_segment_clean.called,
            )

        service.vacuum.start_segment_clean.assert_called_once_with(
            _ROOMS[0]["id"], fan_speed="balanced"
        )

    @pytest.mark.asyncio
    async def test_is_cleaning_flag_set_after_alert(self):
        """is_cleaning is set to True when a cleaning task is launched."""
        service = _make_service()
        monitor = AlertMonitor(areas=["תל אביב"], poll_seconds=_POLL_SECONDS, alert_types=["1"])
        http_mock = _make_http_mock(_TEL_AVIV_ALERT)

        with patch("aiohttp.ClientSession", return_value=http_mock):
            await _run_monitor_until(
                monitor,
                service.on_alert,
                stop_condition=lambda: service.is_cleaning,
            )

        assert service.is_cleaning is True
        # Ensure the task finishes cleanly
        if service.cleaning_task:
            await service.cleaning_task

    @pytest.mark.asyncio
    async def test_full_clean_cycle_docks_and_clears_flag(self):
        """Full cycle: alert → clean starts → duration elapses → dock called → is_cleaning cleared."""
        service = _make_service()
        monitor = AlertMonitor(areas=["תל אביב"], poll_seconds=_POLL_SECONDS, alert_types=["1"])
        http_mock = _make_http_mock(_TEL_AVIV_ALERT)

        with patch("aiohttp.ClientSession", return_value=http_mock):
            await _run_monitor_until(
                monitor,
                service.on_alert,
                stop_condition=lambda: service.vacuum.start_segment_clean.called,
            )

        # Let the short cleaning duration elapse completely
        if service.cleaning_task:
            await service.cleaning_task

        service.vacuum.stop_and_dock.assert_called_once()
        assert service.is_cleaning is False
        service.scheduler.mark_cleaned.assert_called_once_with(_ROOMS[0]["id"])

    @pytest.mark.asyncio
    async def test_notification_sent_on_alert(self):
        """A notification is dispatched when a cleaning session starts."""
        service = _make_service({"notifications": {"enabled": False}})
        monitor = AlertMonitor(areas=["תל אביב"], poll_seconds=_POLL_SECONDS, alert_types=["1"])
        http_mock = _make_http_mock(_TEL_AVIV_ALERT)

        with patch("aiohttp.ClientSession", return_value=http_mock):
            await _run_monitor_until(
                monitor,
                service.on_alert,
                stop_condition=lambda: service.vacuum.start_segment_clean.called,
            )
        if service.cleaning_task:
            await service.cleaning_task

        assert service.notifier.send.called


# ---------------------------------------------------------------------------
# Negative cases: cleaning must NOT start
# ---------------------------------------------------------------------------

class TestE2ENegativeCases:
    @pytest.mark.asyncio
    async def test_empty_api_response_does_not_trigger_clean(self):
        """Empty API response (no active alert) → vacuum never starts."""
        service = _make_service()
        monitor = AlertMonitor(areas=["תל אביב"], poll_seconds=_POLL_SECONDS, alert_types=["1"])
        http_mock = _make_http_mock("")  # no alert active

        with patch("aiohttp.ClientSession", return_value=http_mock):
            await _run_monitor_until(
                monitor,
                service.on_alert,
                stop_condition=lambda: False,  # run for full timeout
                timeout=0.4,
            )

        service.vacuum.start_segment_clean.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_matching_area_does_not_trigger_clean(self):
        """Alert is for Tel Aviv but we're watching Haifa → no cleaning."""
        service = _make_service()
        monitor = AlertMonitor(areas=["חיפה"], poll_seconds=_POLL_SECONDS, alert_types=["1"])
        http_mock = _make_http_mock(_TEL_AVIV_ALERT)

        with patch("aiohttp.ClientSession", return_value=http_mock):
            await _run_monitor_until(
                monitor,
                service.on_alert,
                stop_condition=lambda: False,
                timeout=0.4,
            )

        service.vacuum.start_segment_clean.assert_not_called()

    @pytest.mark.asyncio
    async def test_wrong_alert_category_does_not_trigger_clean(self):
        """Category 3 (earthquake) with matching city → no cleaning (configured for cat 1 only)."""
        service = _make_service()
        monitor = AlertMonitor(areas=["תל אביב"], poll_seconds=_POLL_SECONDS, alert_types=["1"])
        wrong_cat = json.dumps({
            "id": "99999",
            "cat": "3",
            "data": ["תל אביב - מרכז"],
        })
        http_mock = _make_http_mock(wrong_cat)

        with patch("aiohttp.ClientSession", return_value=http_mock):
            await _run_monitor_until(
                monitor,
                service.on_alert,
                stop_condition=lambda: False,
                timeout=0.4,
            )

        service.vacuum.start_segment_clean.assert_not_called()

    @pytest.mark.asyncio
    async def test_duplicate_alert_id_triggers_only_once(self):
        """The same alert ID polled repeatedly must fire the callback exactly once."""
        service = _make_service()
        monitor = AlertMonitor(areas=["תל אביב"], poll_seconds=_POLL_SECONDS, alert_types=["1"])
        http_mock = _make_http_mock(_TEL_AVIV_ALERT)

        with patch("aiohttp.ClientSession", return_value=http_mock):
            # Let the monitor poll multiple times (> 1 poll interval)
            await _run_monitor_until(
                monitor,
                service.on_alert,
                stop_condition=lambda: service.vacuum.start_segment_clean.called,
            )
            # Wait an extra poll cycle with the monitor stopped to ensure no extra calls
            await asyncio.sleep(_POLL_SECONDS * 1.2)

        service.vacuum.start_segment_clean.assert_called_once()

    @pytest.mark.asyncio
    async def test_invalid_json_response_does_not_trigger_clean(self):
        """Malformed JSON from the API → no crash, no cleaning."""
        service = _make_service()
        monitor = AlertMonitor(areas=["תל אביב"], poll_seconds=_POLL_SECONDS, alert_types=["1"])
        http_mock = _make_http_mock("not json at all {{{")

        with patch("aiohttp.ClientSession", return_value=http_mock):
            await _run_monitor_until(
                monitor,
                service.on_alert,
                stop_condition=lambda: False,
                timeout=0.4,
            )

        service.vacuum.start_segment_clean.assert_not_called()
