#!/usr/bin/env python3
"""
mamad_roborock.py — MAMAD Roborock Missile Alert Cleaner

Entry point and async orchestrator.

Usage:
    python mamad_roborock.py [--setup] [--config config.yaml]

Modes:
    --setup   Interactive first-run: authenticates, discovers rooms, prints them.
    (default) Daemon loop: monitors alerts and triggers cleaning.
"""

import argparse
import asyncio
import logging
import logging.handlers
import os
import signal
import sys
from typing import Dict, Any, List, Optional

import yaml

from alert_monitor import AlertMonitor
from notifications import Notifier
from room_scheduler import RoomScheduler
from vacuum_controller import (
    VacuumController,
    STATUS_OK,
    STATUS_ALREADY_CLEANING,
)

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

def _setup_logging(cfg: Dict[str, Any]) -> None:
    level = getattr(logging, cfg.get("log_level", "INFO").upper(), logging.INFO)
    log_file = cfg.get("log_file", "mamad.log")
    max_bytes = cfg.get("log_max_bytes", 5 * 1024 * 1024)
    backup_count = cfg.get("log_backup_count", 3)

    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    handlers: List[logging.Handler] = [logging.StreamHandler(sys.stdout)]

    if log_file:
        os.makedirs(os.path.dirname(log_file) if os.path.dirname(log_file) else ".", exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
        )
        fh.setFormatter(logging.Formatter(fmt))
        handlers.append(fh)

    logging.basicConfig(level=level, format=fmt, handlers=handlers)


log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def _load_config(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
    except FileNotFoundError:
        sys.exit(f"ERROR: Config file not found: {path}\n"
                 "Copy config.yaml to the working directory and fill in your settings.")
    except yaml.YAMLError as exc:
        sys.exit(f"ERROR: Config file parse error: {exc}")

    if not isinstance(cfg, dict):
        sys.exit("ERROR: Config file must be a YAML mapping (dict)")

    return cfg


# ---------------------------------------------------------------------------
# Interactive helpers
# ---------------------------------------------------------------------------

def _prompt_areas(existing: List[str] = None) -> List[str]:
    """Interactively prompt the user to enter (or confirm) the alert areas."""
    print("\n--- Alert Areas ---")
    print("Enter the Hebrew city/area names to watch for alerts.")
    print("Substring matching is used — 'תל אביב' matches 'תל אביב - מרכז' too.")
    print("Separate multiple areas with commas.")
    print("Example: תל אביב, חיפה, באר שבע")
    if existing:
        print(f"\nCurrent areas: {', '.join(existing)}")
        raw = input("Press Enter to keep, or type new areas: ").strip()
        if not raw:
            return existing
    else:
        raw = input("\nAreas: ").strip()

    while True:
        areas = [a.strip() for a in raw.split(",") if a.strip()]
        if areas:
            print(f"Monitoring: {', '.join(areas)}")
            return areas
        raw = input("At least one area is required. Areas: ").strip()


# ---------------------------------------------------------------------------
# Main service
# ---------------------------------------------------------------------------

class MamadService:
    """Async orchestrator: monitors alerts, triggers room cleaning."""

    def __init__(self, cfg: Dict[str, Any]) -> None:
        self.cfg = cfg
        self.rooms: List[Dict] = []
        self.is_cleaning = False
        self.cleaning_task: Optional[asyncio.Task] = None
        self._shutdown_event = asyncio.Event()

        # Sub-components
        self.scheduler = RoomScheduler(
            state_file=cfg.get("state_file", "mamad_state.json"),
            exclude_rooms=cfg.get("exclude_rooms", []),
            cooldown_hours=float(cfg.get("cooldown_hours", 1.0)),
            max_cleans_per_window=int(cfg.get("max_cleans_per_room", 2)),
            clean_window_hours=float(cfg.get("clean_window_hours", 12.0)),
        )
        self.vacuum = VacuumController(
            min_battery_percent=int(cfg.get("min_battery_percent", 20)),
        )
        self.notifier = Notifier(cfg.get("notifications", {}))
        self.alert_monitor: Optional[AlertMonitor] = None

    # ------------------------------------------------------------------
    # Setup mode
    # ------------------------------------------------------------------

    async def run_setup(self) -> None:
        """Interactive first-run: auth, discover rooms, print them."""
        log.info("=== MAMAD Roborock Setup ===")

        # Use stored email if available, otherwise prompt
        email = self.scheduler.get_email()
        if not email:
            email = input("Enter your Roborock account email: ").strip()
            if not email:
                sys.exit("ERROR: Email is required")

        creds = await self.vacuum.setup(
            email=email,
            cached_credentials=self.scheduler.get_cached_credentials(),
            interactive=True,
        )
        self.scheduler.set_email(email)
        self.scheduler.set_cached_credentials(creds)
        self.scheduler.save()

        # Areas setup
        areas = _prompt_areas(existing=self.scheduler.get_areas() or self.cfg.get("areas", []))
        self.scheduler.set_areas(areas)
        self.scheduler.save()

        await self.vacuum.discover_devices()
        rooms = await self.vacuum.discover_rooms()

        if not rooms:
            print("\nWARNING: No rooms discovered. The vacuum may need to complete a mapping run first.")
        else:
            self.scheduler.update_rooms(rooms)
            self.scheduler.save()
            print(f"\nDiscovered {len(rooms)} rooms:")
            for r in rooms:
                print(f"  id={r['id']:>5}  name={r['name']}")

        print("\nSetup complete. You can now start the daemon:")
        print("  python mamad_roborock.py")

    # ------------------------------------------------------------------
    # Daemon mode
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Daemon: authenticate, check state, then monitor alerts."""
        log.info("=== MAMAD Roborock starting (daemon mode) ===")

        # Auth — email is stored in state file after first --setup run
        email = self.scheduler.get_email()
        if not email:
            sys.exit(
                "ERROR: No Roborock account found in state file.\n"
                "Run setup first:  python mamad_roborock.py --setup"
            )
        creds = await self.vacuum.setup(
            email=email,
            cached_credentials=self.scheduler.get_cached_credentials(),
            interactive=False,
        )
        self.scheduler.set_cached_credentials(creds)
        self.scheduler.save()

        await self.vacuum.discover_devices()

        # Resolve areas: config.yaml takes priority, then state file
        areas = self.cfg.get("areas") or self.scheduler.get_areas()
        if not areas:
            sys.exit(
                "ERROR: No alert areas configured.\n"
                "Run setup first:  python mamad_roborock.py --setup"
            )
        self.alert_monitor = AlertMonitor(
            areas=areas,
            poll_seconds=int(self.cfg.get("poll_seconds", 5)),
            alert_types=self.cfg.get("alert_types", ["1"]),
        )

        # Refresh rooms if cache is stale
        await self._refresh_rooms_if_stale()

        if not self.rooms:
            log.error("No rooms available — check setup or re-run with --setup")
            return

        # Check if vacuum is already cleaning on startup
        await self._handle_startup_state()

        # Register graceful shutdown handlers
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self._request_shutdown)

        log.info("Monitoring alerts for areas: %s", self.alert_monitor.areas)
        log.info("Rooms in rotation: %s", [(r["id"], r["name"]) for r in self.rooms])

        # Start alert polling (runs until self._shutdown_event is set)
        monitor_task = asyncio.create_task(
            self.alert_monitor.start(callback=self.on_alert)
        )
        await self._shutdown_event.wait()

        # Graceful shutdown
        log.info("Shutdown requested — stopping...")
        self.alert_monitor.stop()
        monitor_task.cancel()

        if self.cleaning_task and not self.cleaning_task.done():
            log.info("Cancelling active cleaning task...")
            self.cleaning_task.cancel()
            try:
                await self.cleaning_task
            except asyncio.CancelledError:
                pass
            # Best-effort dock
            try:
                await self.vacuum.stop_and_dock()
            except Exception as exc:
                log.error("stop_and_dock on shutdown failed: %s", exc)

        self.scheduler.save()
        await self.vacuum.close()
        log.info("MAMAD Roborock stopped cleanly")

    def _request_shutdown(self) -> None:
        log.info("Signal received — requesting shutdown")
        self._shutdown_event.set()

    # ------------------------------------------------------------------
    # Alert callback
    # ------------------------------------------------------------------

    async def on_alert(self, alert: Dict) -> None:
        """Called by AlertMonitor when a new matching alert fires."""
        alert_id = str(alert.get("id", "?"))
        cities = alert.get("data", [])
        log.info("ALERT id=%s cities=%s", alert_id, cities)

        if self.is_cleaning:
            log.info("Alert ignored — already cleaning (alert id=%s)", alert_id)
            await self.notifier.send(f"Alert received but vacuum is already cleaning (id={alert_id})")
            return

        room = self.scheduler.get_next_room(self.rooms)
        if room is None:
            log.warning("No eligible rooms available for alert id=%s", alert_id)
            await self.notifier.send("Alert received but no eligible rooms available for cleaning")
            return

        status = await self.vacuum.get_status()
        result = status["result"]

        if result != STATUS_OK:
            log.warning("Skipping clean for alert id=%s — vacuum status: %s (state=%s, battery=%d%%)",
                        alert_id, result, status["state"], status["battery"])
            await self.notifier.send(
                f"Alert received but vacuum unavailable: {result} "
                f"(state={status['state']}, battery={status['battery']}%)"
            )
            return

        self.is_cleaning = True
        self.cleaning_task = asyncio.create_task(self._run_clean(room, alert_id))

    # ------------------------------------------------------------------
    # Cleaning session
    # ------------------------------------------------------------------

    async def _run_clean(self, room: Dict, alert_id: str) -> None:
        """Run a timed cleaning session for *room*."""
        duration_seconds = float(self.cfg.get("clean_duration_minutes", 10)) * 60
        fan_speed = self.cfg.get("fan_speed", "balanced")
        room_name = room.get("name", f"Room {room['id']}")

        log.info("Starting cleaning session: room=%s id=%s duration=%.0fs",
                 room_name, room["id"], duration_seconds)
        await self.notifier.send(
            f"🧹 Cleaning *{room_name}* for {self.cfg.get('clean_duration_minutes', 10)} minutes "
            f"(alert id={alert_id})"
        )

        try:
            await self.vacuum.start_segment_clean(room["id"], fan_speed=fan_speed)
            log.info("Cleaning started — waiting %.0f seconds", duration_seconds)
            await asyncio.sleep(duration_seconds)
            log.info("Clean duration elapsed — stopping")
            await self.vacuum.stop_and_dock()

            self.scheduler.mark_cleaned(room["id"])
            self.scheduler.save()

            log.info("Cleaning session complete: room=%s", room_name)
            await self.notifier.send(f"✅ Done cleaning *{room_name}* — returned to dock")

        except asyncio.CancelledError:
            log.info("Cleaning task cancelled for room=%s", room_name)
            raise
        except Exception as exc:
            log.error("Cleaning session failed for room=%s: %s", room_name, exc)
            await self.notifier.send(f"❌ Cleaning *{room_name}* failed: {exc}")
        finally:
            self.is_cleaning = False

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _refresh_rooms_if_stale(self) -> None:
        """Discover rooms from API or load from cache."""
        if self.scheduler.is_room_cache_stale():
            log.info("Room cache is stale — discovering rooms from API")
            try:
                rooms = await self.vacuum.discover_rooms()
                if rooms:
                    self.scheduler.update_rooms(rooms)
                    self.scheduler.save()
            except Exception as exc:
                log.warning("Room discovery failed: %s — using cached data", exc)

        self.rooms = self.scheduler.get_cached_rooms()
        if not self.rooms:
            log.warning("No rooms in cache and discovery failed — run --setup first")

    async def _handle_startup_state(self) -> None:
        """If vacuum is already cleaning at startup, wait for it to finish."""
        try:
            status = await self.vacuum.get_status()
        except Exception as exc:
            log.warning("Could not get vacuum status at startup: %s", exc)
            return

        if status["result"] == STATUS_ALREADY_CLEANING:
            log.info(
                "Vacuum is already cleaning at startup (state=%s) — "
                "ignoring alerts until it finishes",
                status["state"],
            )
            self.is_cleaning = True
            self.cleaning_task = asyncio.create_task(self._wait_for_idle())

    async def _wait_for_idle(self) -> None:
        """Poll until the vacuum is no longer in a cleaning state."""
        poll_interval = 30
        log.info("Waiting for vacuum to finish current session (polling every %ds)", poll_interval)
        while True:
            await asyncio.sleep(poll_interval)
            try:
                status = await self.vacuum.get_status()
                if status["result"] != STATUS_ALREADY_CLEANING:
                    log.info(
                        "Vacuum idle again (state=%s) — resuming alert monitoring",
                        status["state"],
                    )
                    break
            except Exception as exc:
                log.warning("Status poll error while waiting for idle: %s", exc)
        self.is_cleaning = False


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def _test_alert(cfg: Dict[str, Any], scheduler: "RoomScheduler") -> None:
    """Poll the alert API once and show exactly what was returned."""
    import json
    import aiohttp
    from alert_monitor import ALERTS_URL, HEADERS

    areas = cfg.get("areas") or scheduler.get_areas()
    if not areas:
        sys.exit("ERROR: No areas configured. Run --setup first.")
    alert_types = cfg.get("alert_types", ["1"])

    print(f"Polling {ALERTS_URL}")
    print(f"Watching areas: {areas}\n")

    async with aiohttp.ClientSession() as session:
        async with session.get(ALERTS_URL, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            text = (await resp.text(encoding="utf-8-sig")).strip()

    if not text:
        print("No active alert (empty response).")
        return

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        print(f"Non-JSON response: {text!r}")
        return

    print("Raw alert data:")
    print(json.dumps(data, ensure_ascii=False, indent=2))

    cat = str(data.get("cat", ""))
    cities = data.get("data", [])
    cat_match = cat in alert_types
    area_match = any(
        area.lower() in city.lower()
        for area in areas
        for city in cities
    )

    print(f"\nCategory '{cat}' in configured types {alert_types}: {cat_match}")
    print(f"Area match for {areas}: {area_match}")
    if cat_match and area_match:
        print("\n✓ This alert WOULD trigger cleaning.")
    else:
        print("\n✗ This alert would NOT trigger cleaning.")


async def _async_main(args: argparse.Namespace) -> None:
    cfg = _load_config(args.config)
    _setup_logging(cfg)

    service = MamadService(cfg)

    if args.setup:
        await service.run_setup()
    elif args.test_clean is not None:
        await service.run_test_clean(args.test_clean)
    elif args.test_alert:
        await _test_alert(cfg, service.scheduler)
    else:
        await service.run()


async def _run_test_clean(self, room_id: int) -> None:
    """Connect, clean one room for 30 s, then dock. Used by --test-clean."""
    print(f"\nTest clean: room id={room_id} for 30 seconds\n")

    email = self.scheduler.get_email()
    if not email:
        sys.exit("ERROR: Run --setup first")

    await self.vacuum.setup(
        email=email,
        cached_credentials=self.scheduler.get_cached_credentials(),
        interactive=False,
    )
    await self.vacuum.discover_devices()

    status = await self.vacuum.get_status()
    print(f"Vacuum status: state={status['state']} battery={status['battery']}%")
    if status["result"] != STATUS_OK:
        sys.exit(f"ERROR: Vacuum not ready — {status['result']}")

    rooms = self.scheduler.get_cached_rooms()
    room = next((r for r in rooms if r["id"] == room_id), None)
    if room is None:
        available = ", ".join(f"{r['id']}={r['name']}" for r in rooms)
        sys.exit(f"ERROR: Room id={room_id} not found. Available: {available}")

    print(f"Starting segment clean: {room['name']} (id={room_id})")
    await self.vacuum.start_segment_clean(room_id, fan_speed=self.cfg.get("fan_speed", "balanced"))

    print("Cleaning for 30 seconds...")
    await asyncio.sleep(30)

    print("Stopping and returning to dock...")
    await self.vacuum.stop_and_dock()
    await self.vacuum.close()
    print("Done.")


# Attach as method
MamadService.run_test_clean = _run_test_clean


def main() -> None:
    parser = argparse.ArgumentParser(
        description="MAMAD Roborock — Missile Alert Auto-Cleaner"
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        metavar="PATH",
        help="Path to config.yaml (default: config.yaml)",
    )
    parser.add_argument(
        "--setup",
        action="store_true",
        help="Run interactive setup (auth, areas, room discovery). Re-run anytime to change settings.",
    )
    parser.add_argument(
        "--test-clean",
        type=int,
        metavar="ROOM_ID",
        help="Clean a room for 30 s then dock (e.g. --test-clean 16 for Kitchen)",
    )
    parser.add_argument(
        "--test-alert",
        action="store_true",
        help="Poll the alert API once and show whether it matches your configured areas",
    )
    args = parser.parse_args()

    try:
        asyncio.run(_async_main(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
