"""
alert_monitor.py — Pikud HaOref missile alert poller.

Polls the official alerts.json endpoint every `poll_seconds` seconds.
Invokes the registered async callback when a new matching alert is detected.
"""

import asyncio
import json
import logging
import time
from typing import Callable, Awaitable, List

import aiohttp

log = logging.getLogger(__name__)

ALERTS_URL = "https://www.oref.org.il/warningMessages/alert/Alerts.json"
# Returns a JSON array of {"label": "<Hebrew city name>", "value": "..."} objects.
# Uses the alerts-history subdomain which is more permissive than www.oref.org.il.
CITIES_URL = "https://alerts-history.oref.org.il/Shared/Ajax/GetDistricts.aspx?lang=he"
HEADERS = {
    "Referer": "https://www.oref.org.il/11226-he/pakar.aspx",
    "X-Requested-With": "XMLHttpRequest",
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Pragma": "no-cache",
    "Cache-Control": "max-age=0",
}

# City-name substring used by Pikud HaOref for scheduled test drills.
# The live API fires real alerts with this token continuously; most client
# libraries filter them out, but we expose it so callers can opt-in to use
# them as a free end-to-end test signal.
TEST_CITY_TOKEN = "בדיקה"

# How many consecutive network failures before we warn loudly.
_FAILURE_WARN_THRESHOLD = 5


def _cache_bust_url(url: str) -> str:
    """Append a Unix-timestamp query param to defeat CDN/proxy caching."""
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}{int(time.time())}"


def _decode_response(raw: bytes) -> str:
    """Decode an alert API response, handling UTF-16-LE BOM, UTF-8 BOM, and NUL chars."""
    if raw.startswith(b"\xff\xfe"):
        text = raw.decode("utf-16-le")
    elif raw.startswith(b"\xef\xbb\xbf"):
        text = raw[3:].decode("utf-8", errors="replace")
    else:
        text = raw.decode("utf-8", errors="replace")
    return text.replace("\x00", "")


async def fetch_known_areas(session: aiohttp.ClientSession) -> List[str]:
    """
    Return the list of all city/area names recognised by Pikud HaOref.

    The API returns a JSON array of objects; we extract the ``label`` field
    (Hebrew name) from each entry.  On any failure an empty list is returned
    so callers can treat the API as optional.
    """
    try:
        async with session.get(
            _cache_bust_url(CITIES_URL), headers=HEADERS, timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            resp.raise_for_status()
            text = (await resp.text(encoding="utf-8-sig")).strip()
        data = json.loads(text)
        if not isinstance(data, list):
            log.warning("fetch_known_areas: unexpected response type %s", type(data))
            return []
        return [item["label"] for item in data if isinstance(item, dict) and "label" in item]
    except Exception as exc:
        log.warning("fetch_known_areas: could not fetch city list: %s", exc)
        return []


def validate_configured_areas(configured: List[str], known: List[str]) -> List[str]:
    """
    Return the subset of *configured* area strings that do not match any known city.

    Matching uses the same substring/case-insensitive rule as ``_matches_areas``:
    a configured area is considered valid if it appears as a substring in at
    least one known city name.  This means 'תל אביב' is valid because it
    appears inside 'תל אביב - מרכז'.
    """
    known_lower = [k.lower() for k in known]
    return [
        area for area in configured
        if not any(area.lower() in k for k in known_lower)
    ]


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
                            "AlertMonitor: %d consecutive failures — last error: %s: %s",
                            self._consecutive_failures,
                            type(exc).__name__,
                            exc or "(no message)",
                        )
                    else:
                        log.warning("AlertMonitor poll error: %s: %s", type(exc).__name__, exc or "(no message)")
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
        async with session.get(_cache_bust_url(ALERTS_URL), headers=HEADERS, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            resp.raise_for_status()
            raw = await resp.read()
            text = _decode_response(raw)

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
