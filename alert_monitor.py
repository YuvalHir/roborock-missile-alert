"""
alert_monitor.py — Pikud HaOref missile alert poller.

Polls the official alerts.json endpoint every `poll_seconds` seconds.
Invokes the registered async callback when a new matching alert is detected.
"""

import asyncio
import json
import logging
from typing import Callable, Awaitable, List

import aiohttp

log = logging.getLogger(__name__)

ALERTS_URL = "https://www.oref.org.il/WarningMessages/alert/alerts.json"
HEADERS = {
    "Referer": "https://www.oref.org.il/",
    "X-Requested-With": "XMLHttpRequest",
    "Accept": "application/json",
}

# How many consecutive network failures before we warn loudly.
_FAILURE_WARN_THRESHOLD = 5


class AlertMonitor:
    """Polls Pikud HaOref for missile alerts and triggers a callback."""

    def __init__(
        self,
        areas: List[str],
        poll_seconds: int = 5,
        alert_types: List[str] = None,
    ) -> None:
        self.areas = areas
        self.poll_seconds = max(1, poll_seconds)
        self.alert_types = alert_types or ["1"]
        self._last_alert_id: str | None = None
        self._consecutive_failures: int = 0
        self._running = False

    async def start(self, callback: Callable[[dict], Awaitable[None]]) -> None:
        """Poll forever, calling *callback* on each new matching alert."""
        self._running = True
        log.info(
            "AlertMonitor started — areas=%s poll=%ss types=%s",
            self.areas,
            self.poll_seconds,
            self.alert_types,
        )
        async with aiohttp.ClientSession() as session:
            while self._running:
                try:
                    alert = await self._poll(session)
                    if alert:
                        log.info(
                            "New alert id=%s cities=%s",
                            alert.get("id"),
                            alert.get("data"),
                        )
                        await callback(alert)
                except Exception as exc:
                    self._consecutive_failures += 1
                    if self._consecutive_failures >= _FAILURE_WARN_THRESHOLD:
                        log.error(
                            "AlertMonitor: %d consecutive failures — last error: %s",
                            self._consecutive_failures,
                            exc,
                        )
                    else:
                        log.warning("AlertMonitor poll error: %s", exc)
                else:
                    self._consecutive_failures = 0
                await asyncio.sleep(self.poll_seconds)

    def stop(self) -> None:
        """Signal the polling loop to exit after the current sleep."""
        self._running = False

    async def _poll(self, session: aiohttp.ClientSession) -> dict | None:
        """
        Single poll cycle.

        Returns the alert dict if a *new* matching alert is found, else None.
        The API returns either an empty string (no alert) or a JSON object.
        """
        async with session.get(ALERTS_URL, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            resp.raise_for_status()
            text = await resp.text(encoding="utf-8-sig")  # strip BOM if present

        text = text.strip()
        if not text:
            # No active alert
            return None

        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            log.debug("Non-JSON alert response (len=%d): %s", len(text), exc)
            return None

        if not isinstance(data, dict):
            return None

        alert_id = str(data.get("id", ""))
        if alert_id and alert_id == self._last_alert_id:
            log.debug("Skipping duplicate alert id=%s", alert_id)
            return None

        # Check alert category
        cat = str(data.get("cat", ""))
        if cat not in self.alert_types:
            log.debug("Alert cat=%s not in configured types=%s — skipping", cat, self.alert_types)
            return None

        # Check area match (substring, case-insensitive)
        cities: List[str] = data.get("data", [])
        if not self._matches_areas(cities):
            log.debug("Alert cities=%s do not match configured areas — skipping", cities)
            return None

        # New matching alert — record ID and return
        self._last_alert_id = alert_id
        return data

    def _matches_areas(self, cities: List[str]) -> bool:
        """Return True if any configured area string appears in any city name."""
        cities_lower = [c.lower() for c in cities]
        for area in self.areas:
            area_lower = area.lower()
            if any(area_lower in city for city in cities_lower):
                return True
        return False
