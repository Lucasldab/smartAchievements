import math
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from planner import (
    Achievement,
    build_sessions,
    natural_positions,
    plan_campaign,
    project_to_calendar,
)


def _fake_achievements() -> list[Achievement]:
    return [
        Achievement("ACH_TUTORIAL", "Tutorial complete", 95.0),
        Achievement("ACH_MIDGAME", "Main story done", 45.0),
        Achievement("ACH_RARE_BOSS", "Hidden boss", 5.0),
        Achievement("ACH_COMPLETIONIST", "100% collectibles", 2.0),
    ]


def test_natural_positions_without_jitter_invert_rarity():
    rng = random.Random(0)
    positions = natural_positions(_fake_achievements(), jitter_sigma=0.0, rng=rng)
    expected = [0.05, 0.55, 0.95, 0.98]
    assert all(math.isclose(p, e, abs_tol=1e-9) for p, e in zip(positions, expected))


def test_natural_positions_are_clamped_to_unit_interval():
    rng = random.Random(0)
    extreme = [Achievement("A", "A", 0.0), Achievement("B", "B", 100.0)]
    positions = natural_positions(extreme, jitter_sigma=1.0, rng=rng)
    assert all(0.0 <= p <= 1.0 for p in positions)


def test_build_sessions_total_matches_target_hours():
    rng = random.Random(1)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    sessions = build_sessions(10.0, start, rng)
    total = sum((e - s).total_seconds() / 3600.0 for s, e in sessions)
    assert abs(total - 10.0) < 1e-9


def test_build_sessions_have_realistic_gaps():
    rng = random.Random(7)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    sessions = build_sessions(30.0, start, rng)
    assert len(sessions) >= 2
    for i in range(len(sessions) - 1):
        gap_h = (sessions[i + 1][0] - sessions[i][1]).total_seconds() / 3600.0
        assert 12.0 <= gap_h <= 24.0


def test_build_sessions_length_bounded_except_final_truncation():
    rng = random.Random(3)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    sessions = build_sessions(25.0, start, rng)
    for s, e in sessions[:-1]:
        length_h = (e - s).total_seconds() / 3600.0
        assert 2.0 <= length_h <= 4.0
    final_h = (sessions[-1][1] - sessions[-1][0]).total_seconds() / 3600.0
    assert 0.0 < final_h <= 4.0


def test_project_to_calendar_lands_inside_sessions():
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    sessions = [
        (start, start + timedelta(hours=3)),
        (start + timedelta(hours=24), start + timedelta(hours=26)),
    ]
    assert project_to_calendar(0.0, sessions) == start
    assert project_to_calendar(2.0, sessions) == start + timedelta(hours=2)
    assert project_to_calendar(4.0, sessions) == start + timedelta(hours=25)
    assert project_to_calendar(5.0, sessions) == sessions[-1][1]


def test_plan_campaign_orders_by_rarity_without_jitter():
    start = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    unlocks = plan_campaign(
        _fake_achievements(),
        target_hours=10.0,
        start=start,
        seed=42,
        jitter_sigma=0.0,
    )
    by_name = {u.api_name: u for u in unlocks}
    assert by_name["ACH_TUTORIAL"].in_game_hour < by_name["ACH_MIDGAME"].in_game_hour
    assert by_name["ACH_MIDGAME"].in_game_hour < by_name["ACH_RARE_BOSS"].in_game_hour
    assert by_name["ACH_RARE_BOSS"].in_game_hour < by_name["ACH_COMPLETIONIST"].in_game_hour


def test_plan_campaign_is_sorted_by_calendar_time():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    unlocks = plan_campaign(_fake_achievements(), 15.0, start, seed=0)
    for a, b in zip(unlocks, unlocks[1:]):
        assert a.unlock_at <= b.unlock_at


def test_plan_campaign_is_deterministic_under_seed():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    a = plan_campaign(_fake_achievements(), 10.0, start, seed=123)
    b = plan_campaign(_fake_achievements(), 10.0, start, seed=123)
    assert a == b


def test_jitter_preserves_approximate_rarity_order():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for seed in range(20):
        unlocks = plan_campaign(_fake_achievements(), 10.0, start, seed=seed, jitter_sigma=0.05)
        by_name = {u.api_name: u for u in unlocks}
        assert by_name["ACH_TUTORIAL"].in_game_hour < by_name["ACH_COMPLETIONIST"].in_game_hour


if __name__ == "__main__":
    import traceback

    tests = [(name, fn) for name, fn in globals().items()
             if name.startswith("test_") and callable(fn)]
    failures = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS {name}")
        except Exception:
            failures += 1
            print(f"FAIL {name}")
            traceback.print_exc()
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    sys.exit(1 if failures else 0)
