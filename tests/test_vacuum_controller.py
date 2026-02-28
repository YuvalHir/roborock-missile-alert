"""
Tests for VacuumController — status classification, constants, error handling.

Hardware-dependent methods (discover_devices, start_segment_clean, etc.) are
tested with mocked device objects. Authentication flow is not tested here as
it requires network access.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from vacuum_controller import (
    VacuumController,
    STATUS_OK,
    STATUS_ALREADY_CLEANING,
    STATUS_LOW_BATTERY,
    STATUS_ERROR,
    _FAN_SPEED_MAP,
    _CLEANING_STATE_VALUES,
)


# ---------------------------------------------------------------------------
# Constants & maps
# ---------------------------------------------------------------------------

class TestConstants:
    def test_status_constants_are_strings(self):
        assert isinstance(STATUS_OK, str)
        assert isinstance(STATUS_ALREADY_CLEANING, str)
        assert isinstance(STATUS_LOW_BATTERY, str)
        assert isinstance(STATUS_ERROR, str)

    def test_all_fan_speeds_present(self):
        for name in ("quiet", "balanced", "turbo", "max", "max_plus"):
            assert name in _FAN_SPEED_MAP

    def test_fan_speed_values_are_ints(self):
        for val in _FAN_SPEED_MAP.values():
            assert isinstance(val, int)

    def test_cleaning_state_values_nonempty(self):
        assert len(_CLEANING_STATE_VALUES) > 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_status_device(state_value=3, battery=80, error_value=0):
    """Build a mock device with a status trait."""
    state_code = MagicMock()
    state_code.name = "idle"
    state_code.value = state_value

    error_code = MagicMock()
    error_code.value = error_value

    status_trait = AsyncMock()
    status_trait.state = state_code
    status_trait.battery = battery
    status_trait.error_code = error_code
    # refresh() is already an awaitable via AsyncMock

    device = MagicMock()
    device.v1_properties.status = status_trait
    return device


# ---------------------------------------------------------------------------
# VacuumController.__init__
# ---------------------------------------------------------------------------

class TestInit:
    def test_default_min_battery(self):
        vc = VacuumController()
        assert vc.min_battery_percent == 30

    def test_custom_min_battery(self):
        vc = VacuumController(min_battery_percent=35)
        assert vc.min_battery_percent == 35

    def test_device_starts_as_none(self):
        vc = VacuumController()
        assert vc._device is None


# ---------------------------------------------------------------------------
# get_status — classification logic
# ---------------------------------------------------------------------------

class TestGetStatus:
    @pytest.mark.asyncio
    async def test_ok_when_idle(self):
        vc = VacuumController(min_battery_percent=20)
        vc._device = make_status_device(state_value=3, battery=80, error_value=0)
        result = await vc.get_status()
        assert result["result"] == STATUS_OK
        assert result["battery"] == 80

    @pytest.mark.asyncio
    async def test_low_battery_when_below_threshold(self):
        vc = VacuumController(min_battery_percent=20)
        vc._device = make_status_device(state_value=3, battery=10, error_value=0)
        result = await vc.get_status()
        assert result["result"] == STATUS_LOW_BATTERY

    @pytest.mark.asyncio
    async def test_already_cleaning_for_cleaning_state(self):
        vc = VacuumController(min_battery_percent=20)
        cleaning_state = next(iter(_CLEANING_STATE_VALUES))
        vc._device = make_status_device(state_value=cleaning_state, battery=80, error_value=0)
        result = await vc.get_status()
        assert result["result"] == STATUS_ALREADY_CLEANING

    @pytest.mark.asyncio
    async def test_error_when_error_code_nonzero(self):
        vc = VacuumController(min_battery_percent=20)
        vc._device = make_status_device(state_value=3, battery=80, error_value=5)
        result = await vc.get_status()
        assert result["result"] == STATUS_ERROR

    @pytest.mark.asyncio
    async def test_error_when_state_is_12(self):
        vc = VacuumController(min_battery_percent=20)
        vc._device = make_status_device(state_value=12, battery=80, error_value=0)
        result = await vc.get_status()
        assert result["result"] == STATUS_ERROR

    @pytest.mark.asyncio
    async def test_raises_without_device(self):
        vc = VacuumController()
        with pytest.raises(RuntimeError):
            await vc.get_status()

    @pytest.mark.asyncio
    async def test_battery_defaults_to_100_when_none(self):
        vc = VacuumController(min_battery_percent=20)
        device = make_status_device(state_value=3, battery=None, error_value=0)
        vc._device = device
        result = await vc.get_status()
        assert result["battery"] == 100


# ---------------------------------------------------------------------------
# stop_and_dock
# ---------------------------------------------------------------------------

class TestStopAndDock:
    @pytest.mark.asyncio
    async def test_no_device_returns_immediately(self):
        vc = VacuumController()
        await vc.stop_and_dock()  # must not raise

    @pytest.mark.asyncio
    async def test_stop_error_is_swallowed(self):
        vc = VacuumController()
        device = MagicMock()
        device.v1_properties.command.send = AsyncMock(side_effect=[Exception("stop failed"), None])
        vc._device = device
        await vc.stop_and_dock()  # must not raise


# ---------------------------------------------------------------------------
# close
# ---------------------------------------------------------------------------

class TestClose:
    @pytest.mark.asyncio
    async def test_close_no_device_manager(self):
        vc = VacuumController()
        await vc.close()  # must not raise

    @pytest.mark.asyncio
    async def test_close_calls_device_manager_close(self):
        vc = VacuumController()
        dm = MagicMock()
        dm.close = AsyncMock()
        vc._device_manager = dm
        await vc.close()
        dm.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_close_swallows_exception(self):
        vc = VacuumController()
        dm = MagicMock()
        dm.close = AsyncMock(side_effect=Exception("boom"))
        vc._device_manager = dm
        await vc.close()  # must not raise


# ---------------------------------------------------------------------------
# discover_rooms / discover_devices — guard checks
# ---------------------------------------------------------------------------

class TestGuards:
    @pytest.mark.asyncio
    async def test_discover_rooms_raises_without_device(self):
        vc = VacuumController()
        with pytest.raises(RuntimeError, match="discover_devices"):
            await vc.discover_rooms()

    @pytest.mark.asyncio
    async def test_discover_devices_raises_without_setup(self):
        vc = VacuumController()
        with pytest.raises(RuntimeError, match="setup"):
            await vc.discover_devices()
