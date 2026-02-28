"""
notifications.py — Optional alert/status notifications.

Supports Telegram and ntfy providers.
If notifications are disabled in config, all calls are no-ops.
"""

import logging
from typing import Dict, Any

import aiohttp

log = logging.getLogger(__name__)


class Notifier:
    """Send notifications via Telegram or ntfy. No-op when disabled."""

    def __init__(self, config: Dict[str, Any]) -> None:
        self._enabled = bool(config.get("enabled", False))
        self._provider = config.get("provider", "telegram")
        self._telegram = config.get("telegram", {})
        self._ntfy = config.get("ntfy", {})

        if self._enabled:
            log.info("Notifications enabled via provider=%s", self._provider)
        else:
            log.info("Notifications disabled")

    async def send(self, message: str) -> None:
        """Send *message* via the configured provider."""
        if not self._enabled:
            return
        try:
            if self._provider == "telegram":
                await self._send_telegram(message)
            elif self._provider == "ntfy":
                await self._send_ntfy(message)
            else:
                log.warning("Unknown notification provider: %s", self._provider)
        except Exception as exc:
            log.error("Notification failed (%s): %s", self._provider, exc)

    async def _send_telegram(self, message: str) -> None:
        token = self._telegram.get("bot_token", "")
        chat_id = self._telegram.get("chat_id", "")
        if not token or not chat_id:
            log.warning("Telegram not configured (missing bot_token or chat_id)")
            return
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {"chat_id": chat_id, "text": message, "parse_mode": "Markdown"}
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                resp.raise_for_status()
        log.debug("Telegram notification sent")

    async def _send_ntfy(self, message: str) -> None:
        topic = self._ntfy.get("topic", "mamad-roborock")
        server = self._ntfy.get("server", "https://ntfy.sh").rstrip("/")
        url = f"{server}/{topic}"
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                data=message.encode("utf-8"),
                headers={"Title": "MAMAD Roborock"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                resp.raise_for_status()
        log.debug("ntfy notification sent to %s", url)
