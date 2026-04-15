import math
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from planner import (
    Achievement,
    build_sessions,
    detect_time_requirement_hours,
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


def test_detect_time_requirement_hours_basic():
    assert detect_time_requirement_hours("Be a Coconut for 1 hour (Score 3600)") == 1.0
    assert detect_time_requirement_hours("Be a Coconut for 1000 hours (Score 3600000)") == 1000.0
    assert detect_time_requirement_hours("Play for 10 hours") == 10.0
    assert detect_time_requirement_hours("Survive for 30 minutes") == 0.5
    assert detect_time_requirement_hours("Stay alive for 2 days") == 48.0


def test_detect_time_requirement_hours_rejects_non_time_gates():
    # no trigger verb
    assert detect_time_requirement_hours("Sprint to victory in under 365 days") is None
    # trigger verb but no cumulative 'for'
    assert detect_time_requirement_hours("Win 5 times in 10 minutes") is None
    # activity, not playtime
    assert detect_time_requirement_hours("Play a multiplayer VS game to completion") is None
    # empty or missing
    assert detect_time_requirement_hours("") is None
    assert detect_time_requirement_hours("Kill 1000 zombies") is None


def test_detect_time_requirement_hours_handles_intervening_words():
    assert detect_time_requirement_hours("Play for a total of 50 hours") == 50.0
    assert detect_time_requirement_hours("Be idle for at least 3 hours in the game") == 3.0


def _coconut_like() -> list[Achievement]:
    return [
        Achievement("Ach1", "Beginner Coconut", 29.5, time_requirement_hours=1.0),
        Achievement("Ach2", "Noob Coconut", 17.0, time_requirement_hours=5.0),
        Achievement("Ach3", "Ordinary Coconut", 12.9, time_requirement_hours=10.0),
        Achievement("Ach4", "Unusual Coconut", 5.9, time_requirement_hours=50.0),
        Achievement("Ach5", "Legendary Coconut", 4.2, time_requirement_hours=100.0),
    ]


def test_plan_campaign_time_gated_fires_at_required_hour():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    unlocks = plan_campaign(_coconut_like(), target_hours=100.0, start=start, seed=0)
    by_name = {u.api_name: u for u in unlocks}
    # every time-gated achievement's in_game_hour must equal its requirement
    assert by_name["Ach1"].in_game_hour == 1.0
    assert by_name["Ach2"].in_game_hour == 5.0
    assert by_name["Ach3"].in_game_hour == 10.0
    assert by_name["Ach4"].in_game_hour == 50.0
    assert by_name["Ach5"].in_game_hour == 100.0


def test_plan_campaign_includes_time_gates_beyond_target():
    """Time-gated achievements past target hours are still scheduled — the
    orchestrator gates firing on real playtime, so over-budget requirements
    simply park at the last session's calendar slot until playtime catches up.
    Regression: previously these were dropped, stranding long-haul unlocks."""
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    unlocks = plan_campaign(_coconut_like(), target_hours=20.0, start=start, seed=0)
    by_name = {u.api_name: u for u in unlocks}
    # all five are scheduled, regardless of target
    assert set(by_name) == {"Ach1", "Ach2", "Ach3", "Ach4", "Ach5"}
    # in_game_hour preserves the absolute playtime requirement even past target
    assert by_name["Ach4"].in_game_hour == 50.0
    assert by_name["Ach5"].in_game_hour == 100.0


def test_plan_campaign_mixed_time_and_rarity():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    mixed = [
        Achievement("TG_EARLY", "Play 1h", 50.0, time_requirement_hours=1.0),
        Achievement("TG_LATE", "Play 9h", 2.0, time_requirement_hours=9.0),
        Achievement("RARITY_COMMON", "Tutorial", 95.0),  # rarity -> ~0.05 * 10 = 0.5h
        Achievement("RARITY_RARE", "Final", 5.0),  # rarity -> ~0.95 * 10 = 9.5h
    ]
    unlocks = plan_campaign(mixed, target_hours=10.0, start=start, seed=42, jitter_sigma=0.0)
    by_name = {u.api_name: u for u in unlocks}
    assert by_name["TG_EARLY"].in_game_hour == 1.0
    assert by_name["TG_LATE"].in_game_hour == 9.0
    # rarity-based achievements still respect the inverted-rarity mapping
    assert by_name["RARITY_COMMON"].in_game_hour < by_name["RARITY_RARE"].in_game_hour


def test_plan_campaign_preserves_absolute_playtime_as_in_game_hour():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    unlocks = plan_campaign(
        _coconut_like(),
        target_hours=100.0,
        start=start,
        seed=0,
        baseline_playtime_hours=3.0,
    )
    by_name = {u.api_name: u for u in unlocks}
    # in_game_hour is always the absolute playtime target, regardless of
    # whether the achievement is overdue relative to current playtime
    assert by_name["Ach1"].in_game_hour == 1.0  # overdue
    assert by_name["Ach2"].in_game_hour == 5.0
    assert by_name["Ach3"].in_game_hour == 10.0
    assert by_name["Ach4"].in_game_hour == 50.0
    assert by_name["Ach5"].in_game_hour == 100.0


def test_plan_campaign_overdue_time_gates_fire_near_start():
    start = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    # current playtime of 8h means Ach1 (1h) and Ach2 (5h) are overdue.
    unlocks = plan_campaign(
        _coconut_like(),
        target_hours=100.0,
        start=start,
        seed=0,
        baseline_playtime_hours=8.0,
    )
    by_name = {u.api_name: u for u in unlocks}
    # overdue achievements should unlock in the first session (within 4h of start)
    ach1_dt = datetime.fromisoformat(by_name["Ach1"].unlock_at)
    ach2_dt = datetime.fromisoformat(by_name["Ach2"].unlock_at)
    assert (ach1_dt - start).total_seconds() < 4 * 3600
    assert (ach2_dt - start).total_seconds() < 4 * 3600
    # Ach3 (10h requirement, 2h remaining) is not overdue and fires later
    ach3_dt = datetime.fromisoformat(by_name["Ach3"].unlock_at)
    assert ach3_dt > ach1_dt
    assert ach3_dt > ach2_dt


def test_plan_campaign_current_playtime_shortens_calendar():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    # no current playtime: schedule spans the full window
    fresh = plan_campaign(
        _coconut_like(), target_hours=100.0, start=start, seed=0,
        baseline_playtime_hours=0.0,
    )
    # with 40h already played, remaining window is ~60h
    partial = plan_campaign(
        _coconut_like(), target_hours=100.0, start=start, seed=0,
        baseline_playtime_hours=40.0,
    )
    fresh_end = max(datetime.fromisoformat(u.unlock_at) for u in fresh)
    partial_end = max(datetime.fromisoformat(u.unlock_at) for u in partial)
    assert partial_end < fresh_end


def test_plan_campaign_rarity_overdue_achievements_also_stagger_near_start():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    # rarity 95% -> position ~0.05 -> absolute 5h (with target 100h)
    # current playtime 20h means this is already overdue
    achs = [
        Achievement("EARLY_RARE", "Tutorial", 95.0),
        Achievement("LATE_RARE", "Final", 5.0),
    ]
    unlocks = plan_campaign(
        achs, target_hours=100.0, start=start, seed=0, jitter_sigma=0.0,
        baseline_playtime_hours=20.0,
    )
    by_name = {u.api_name: u for u in unlocks}
    early_dt = datetime.fromisoformat(by_name["EARLY_RARE"].unlock_at)
    late_dt = datetime.fromisoformat(by_name["LATE_RARE"].unlock_at)
    # EARLY_RARE is overdue (absolute 5h < 20h current), fires near start
    assert (early_dt - start).total_seconds() < 4 * 3600
    # LATE_RARE is still future (absolute 95h > 20h current), fires much later
    assert late_dt > early_dt + timedelta(days=1)


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
