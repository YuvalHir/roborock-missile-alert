"""
Tests for RoomScheduler — round-robin, exclusions, cooldown, persistence.
"""

import json
import os
import tempfile
from datetime import datetime, timezone, timedelta

import pytest

from room_scheduler import RoomScheduler

ROOMS = [
    {"id": 1, "name": "Kitchen"},
    {"id": 2, "name": "Living Room"},
    {"id": 3, "name": "Bedroom"},
    {"id": 4, "name": "Mamad"},
]


@pytest.fixture
def state_file(tmp_path):
    return str(tmp_path / "state.json")


@pytest.fixture
def scheduler(state_file):
    return RoomScheduler(state_file=state_file)


# ---------------------------------------------------------------------------
# Round-robin
# ---------------------------------------------------------------------------

class TestRoundRobin:
    def test_advances_index(self, scheduler):
        rooms = [{"id": 1, "name": "A"}, {"id": 2, "name": "B"}, {"id": 3, "name": "C"}]
        assert scheduler.get_next_room(rooms)["id"] == 1
        assert scheduler.get_next_room(rooms)["id"] == 2
        assert scheduler.get_next_room(rooms)["id"] == 3

    def test_wraps_around(self, scheduler):
        rooms = [{"id": 1, "name": "A"}, {"id": 2, "name": "B"}]
        scheduler.get_next_room(rooms)
        scheduler.get_next_room(rooms)
        assert scheduler.get_next_room(rooms)["id"] == 1

    def test_returns_none_for_empty_list(self, scheduler):
        assert scheduler.get_next_room([]) is None


# ---------------------------------------------------------------------------
# Mamad exclusion
# ---------------------------------------------------------------------------

class TestMamadExclusion:
    @pytest.mark.parametrize("name", ["Mamad", "mamad", "MAMAD", "ממד", 'ממ"ד', "ממ״ד"])
    def test_excludes_mamad_variants(self, scheduler, name):
        rooms = [{"id": 1, "name": "Kitchen"}, {"id": 2, "name": name}]
        for _ in range(10):
            room = scheduler.get_next_room(rooms)
            assert room is not None
            assert room["name"] != name

    def test_does_not_exclude_partial_match(self, scheduler):
        rooms = [{"id": 1, "name": "Corridor (mamad side)"}]
        room = scheduler.get_next_room(rooms)
        assert room is not None
        assert room["id"] == 1

    def test_full_mamad_rotation_skipped(self, scheduler):
        rooms = [{"id": 1, "name": "Mamad"}]
        assert scheduler.get_next_room(rooms) is None


# ---------------------------------------------------------------------------
# User exclusion list
# ---------------------------------------------------------------------------

class TestUserExclusions:
    def test_excludes_by_substring(self, state_file):
        s = RoomScheduler(state_file=state_file, exclude_rooms=["bathroom", "balcony"])
        rooms = [
            {"id": 1, "name": "Kitchen"},
            {"id": 2, "name": "Master Bathroom"},
            {"id": 3, "name": "Balcony"},
        ]
        for _ in range(10):
            room = s.get_next_room(rooms)
            assert room["id"] == 1

    def test_exclusion_is_case_insensitive(self, state_file):
        s = RoomScheduler(state_file=state_file, exclude_rooms=["BATH"])
        rooms = [{"id": 1, "name": "bathroom"}, {"id": 2, "name": "Kitchen"}]
        for _ in range(10):
            room = s.get_next_room(rooms)
            assert room["id"] == 2


# ---------------------------------------------------------------------------
# Cooldown
# ---------------------------------------------------------------------------

class TestCooldown:
    def test_room_on_cooldown_is_skipped(self, state_file):
        s = RoomScheduler(state_file=state_file, cooldown_hours=1)
        rooms = [{"id": 1, "name": "A"}, {"id": 2, "name": "B"}]
        s.mark_cleaned(1)
        # Room 1 should be on cooldown; only room 2 available
        for _ in range(4):
            room = s.get_next_room(rooms)
            assert room["id"] == 2

    def test_room_available_after_cooldown(self, state_file):
        s = RoomScheduler(state_file=state_file, cooldown_hours=1)
        rooms = [{"id": 1, "name": "A"}]
        # Set last_cleaned to 2 hours ago
        past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        s._state["last_cleaned"]["1"] = past
        room = s.get_next_room(rooms)
        assert room["id"] == 1

    def test_all_rooms_on_cooldown_returns_none(self, state_file):
        s = RoomScheduler(state_file=state_file, cooldown_hours=1)
        rooms = [{"id": 1, "name": "A"}, {"id": 2, "name": "B"}]
        s.mark_cleaned(1)
        s.mark_cleaned(2)
        assert s.get_next_room(rooms) is None


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

class TestPersistence:
    def test_save_and_reload(self, state_file):
        s = RoomScheduler(state_file=state_file)
        rooms = [{"id": 1, "name": "A"}, {"id": 2, "name": "B"}]
        s.get_next_room(rooms)  # advance index to 1
        s.mark_cleaned(1)
        s.save()

        s2 = RoomScheduler(state_file=state_file)
        assert s2._state["round_robin_index"] == 1
        assert "1" in s2._state["last_cleaned"]

    def test_corrupt_state_file_resets(self, state_file):
        with open(state_file, "w") as f:
            f.write("not valid json {{{")
        s = RoomScheduler(state_file=state_file)
        assert s._state["round_robin_index"] == 0

    def test_missing_state_file_starts_fresh(self, state_file):
        assert not os.path.exists(state_file)
        s = RoomScheduler(state_file=state_file)
        assert s._state["round_robin_index"] == 0

    def test_save_creates_file_with_restricted_permissions(self, state_file):
        s = RoomScheduler(state_file=state_file)
        s.save()
        mode = oct(os.stat(state_file).st_mode)[-3:]
        assert mode == "600"


# ---------------------------------------------------------------------------
# Room cache staleness
# ---------------------------------------------------------------------------

class TestRoomCache:
    def test_stale_when_never_discovered(self, scheduler):
        assert scheduler.is_room_cache_stale() is True

    def test_not_stale_after_update(self, scheduler):
        scheduler.update_rooms([{"id": 1, "name": "A"}])
        assert scheduler.is_room_cache_stale() is False

    def test_stale_after_cache_hours_exceeded(self, state_file):
        s = RoomScheduler(state_file=state_file, room_cache_hours=1)
        past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        s._state["last_room_discovery"] = past
        assert s.is_room_cache_stale() is True


# ---------------------------------------------------------------------------
# Areas and email storage
# ---------------------------------------------------------------------------

class TestStoredConfig:
    def test_set_and_get_areas(self, scheduler):
        scheduler.set_areas(["תל אביב", "חיפה"])
        assert scheduler.get_areas() == ["תל אביב", "חיפה"]

    def test_set_and_get_email(self, scheduler):
        scheduler.set_email("test@example.com")
        assert scheduler.get_email() == "test@example.com"

    def test_areas_persisted(self, state_file):
        s = RoomScheduler(state_file=state_file)
        s.set_areas(["קדימה"])
        s.save()
        s2 = RoomScheduler(state_file=state_file)
        assert s2.get_areas() == ["קדימה"]
