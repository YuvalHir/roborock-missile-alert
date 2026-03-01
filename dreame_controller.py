"""
dreame_controller.py — Dreame wrapper using Tasshack/dreame-vacuum beta protocol.

Handles:
  - Dreame cloud authentication
  - Device and room discovery
  - Pre-clean status checks (battery, state, errors)
  - Segment cleaning and stop/dock commands
"""

import asyncio
import logging
from typing import Dict, Any, List

from dreame.device import DreameVacuumDevice
from dreame.types import (
    DreameVacuumState,
    DreameVacuumProperty,
    DreameVacuumAction,
)
from dreame.protocol import DreameVacuumProtocol

log = logging.getLogger(__name__)

# Result strings returned by get_status()
STATUS_OK = "ok"
STATUS_ALREADY_CLEANING = "already_cleaning"
STATUS_LOW_BATTERY = "low_battery"
STATUS_ERROR = "error"

# States that count as actively cleaning (from Tasshack's dreame integration)
_CLEANING_STATES = {
    DreameVacuumState.CLEANING,
    DreameVacuumState.ZONE_CLEANING,
    DreameVacuumState.SEGMENT_CLEANING,
    DreameVacuumState.SPOT_CLEANING,
    DreameVacuumState.CUSTOM_CLEANING,
}

class DreameController:
    """Async wrapper around DreameVacuumDevice from Tasshack."""

    def __init__(self, min_battery_percent: int = 30) -> None:
        self.min_battery_percent = min_battery_percent
        self._username: str | None = None
        self._password: str | None = None
        self._country: str | None = None
        self._device: DreameVacuumDevice | None = None
        self._host: str | None = None
        self._token: str | None = None

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    async def setup(
        self,
        username: str,
        password: str = None,
        country: str = "cn",
        cached_credentials: Dict = None,
        interactive: bool = True,
    ) -> Dict:
        """
        Authenticate with the Dreame/Xiaomi cloud.
        """
        self._username = username
        self._country = country

        if cached_credentials:
            self._password = cached_credentials.get("password")
            self._host = cached_credentials.get("host")
            self._token = cached_credentials.get("token")
            log.info("Restored Dreame session from cache")
            if self._password and self._host and self._token:
                return cached_credentials

        if not interactive and not self._password:
            raise RuntimeError(
                "No valid cached credentials and interactive=False.\n"
                "Run first:  python mamad_roborock.py --setup"
            )

        if not self._password:
            import getpass
            self._password = getpass.getpass(f"Enter password for Xiaomi account {username}: ")

        log.info("Login info provided for %s. (Device discovery will be performed next.)", username)

        return {
            "username": self._username,
            "password": self._password,
            "country": self._country,
            "host": self._host,
            "token": self._token
        }

    # ------------------------------------------------------------------
    # Device & room discovery
    # ------------------------------------------------------------------

    async def discover_devices(self) -> None:
        """Create the device and connect to it."""
        if not self._username or not self._password:
            raise RuntimeError("Call setup() before discover_devices()")

        # For this standalone controller without HA config flow, we initiate
        # DreameVacuumProtocol to fetch device lists from cloud.
        protocol = DreameVacuumProtocol(
            username=self._username,
            password=self._password,
            country=self._country,
            account_type="mi"
        )

        try:
            log.info("Logging into cloud...")
            await asyncio.get_event_loop().run_in_executor(None, protocol.login)
            log.info("Fetching devices...")
            devices = await asyncio.get_event_loop().run_in_executor(None, protocol.get_devices)
        except Exception as exc:
            raise RuntimeError(f"Failed to fetch devices from cloud: {exc}")

        if not devices:
            raise RuntimeError("No Dreame devices found on this account")

        # Pick the first device
        device_info = devices[0]
        self._host = device_info.get("localip")
        self._token = device_info.get("token")

        name = device_info.get("name", "Unknown Dreame Vacuum")
        mac = device_info.get("mac", "")

        log.info("Using device: %s (host=%s)", name, self._host)

        self._device = DreameVacuumDevice(
            name=name,
            host=self._host,
            token=self._token,
            mac=mac,
            username=self._username,
            password=self._password,
            country=self._country,
            prefer_cloud=True,
            account_type="mi",
            device_id=device_info.get("did")
        )

        try:
            log.info("Connecting to device and updating properties...")
            await asyncio.get_event_loop().run_in_executor(None, self._device.update)
            # Fetch maps
            if self._device.map_manager:
                await asyncio.get_event_loop().run_in_executor(None, self._device.map_manager.update)
        except Exception as exc:
            log.warning("Initial device connection error: %s", exc)

    async def discover_rooms(self) -> List[Dict]:
        """
        Return a list of ``{"id": int, "name": str}`` dicts for all rooms.
        """
        if self._device is None:
            raise RuntimeError("Call discover_devices() before discover_rooms()")

        if not self._device.map_manager or not self._device.status.current_map:
            log.warning("No map found on device. Returning empty room list.")
            return []

        rooms = []
        try:
            # Map elements
            for seg_id, segment in self._device.status.current_map.segments.items():
                rooms.append({"id": seg_id, "name": segment.name or f"Room {seg_id}"})

            log.info(
                "Discovered %d rooms: %s",
                len(rooms),
                [(r["id"], r["name"]) for r in rooms],
            )
        except Exception as exc:
            log.warning("Room discovery failed: %s", exc)

        return rooms

    # ------------------------------------------------------------------
    # Status & commands
    # ------------------------------------------------------------------

    async def get_status(self) -> Dict[str, Any]:
        """
        Refresh and return vacuum status.

        Returns dict with keys: state (str), battery (int), error_code (int), result (str).
        """
        if self._device is None:
            raise RuntimeError("Call discover_devices() first")

        await asyncio.get_event_loop().run_in_executor(None, self._device.update)

        state = self._device.status.state
        battery = self._device.status.battery_level
        has_error = self._device.status.has_error

        state_name = state.name if state else "unknown"
        state_value = state.value if state else 0

        log.debug(
            "Vacuum status: state=%s battery=%d error=%s",
            state_name, battery, has_error
        )

        if has_error:
            result = STATUS_ERROR
        elif state in _CLEANING_STATES:
            result = STATUS_ALREADY_CLEANING
        elif battery < self.min_battery_percent:
            result = STATUS_LOW_BATTERY
        else:
            result = STATUS_OK

        return {
            "state": state_name,
            "battery": battery,
            "error_code": 1 if has_error else 0,
            "result": result,
        }

    async def start_segment_clean(self, segment_id: int, fan_speed: str = "balanced") -> None:
        """Start cleaning a single segment (room)."""
        if self._device is None:
            raise RuntimeError("Call discover_devices() first")

        log.info("Starting segment clean: segment_id=%d", segment_id)

        # Mapping from string to the integer values if applicable
        # The Tasshack library usually accepts ints, but we can pass suction_level mapped
        # Assuming we just trigger clean_segment and let it use defaults
        await asyncio.get_event_loop().run_in_executor(
            None,
            self._device.clean_segment,
            [segment_id],
            1, # cleaning_times
            None, # suction_level
            None, # water_volume
        )

    async def stop_and_dock(self) -> None:
        """Stop the current job and send the vacuum home."""
        if self._device is None:
            return

        log.info("Stopping and returning to dock")
        try:
            await asyncio.get_event_loop().run_in_executor(None, self._device.stop)
            await asyncio.sleep(2)
            await asyncio.get_event_loop().run_in_executor(None, self._device.return_to_base)
        except Exception as exc:
            log.warning("stop_and_dock error: %s", exc)

    async def close(self) -> None:
        """Close connections."""
        if self._device is not None:
            self._device.disconnect()
