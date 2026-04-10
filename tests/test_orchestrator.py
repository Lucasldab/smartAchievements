import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from orchestrator import (
    MAX_VERIFY_ATTEMPTS,
    Orchestrator,
    SteamState,
    init_db,
    make_subprocess_fire,
)


class FakeSteam:
    def __init__(self):
        self.state_by_appid: dict[int, SteamState] = {}
        self.calls = 0

    def get_state(self, appid: int) -> SteamState:
        self.calls += 1
        return self.state_by_appid.get(appid, SteamState(playtime_hours=0.0))


def _mem_db() -> sqlite3.Connection:
    return init_db(":memory:")


def _make_schedule_file(tmp_path: Path, appid: int, unlocks: list[dict], **overrides) -> Path:
    payload = {
        "appid": appid,
        "target_hours": 10.0,
        "hours_source": "manual",
        "current_playtime_hours": 0.0,
        "planned_start": "2026-04-10T00:00:00+00:00",
        "seed": 0,
        "jitter_sigma": 0.05,
        "unlocks": unlocks,
        **overrides,
    }
    p = tmp_path / "schedule.json"
    p.write_text(json.dumps(payload))
    return p


def _unlock(api_name: str, unlock_at: str, **kwargs) -> dict:
    return {
        "api_name": api_name,
        "display_name": kwargs.get("display_name", api_name),
        "global_percent": kwargs.get("global_percent", 50.0),
        "in_game_hour": kwargs.get("in_game_hour", 1.0),
        "unlock_at": unlock_at,
        "time_requirement_hours": kwargs.get("time_requirement_hours"),
    }


def _fake_fire(calls: list) -> callable:
    def f(appid, api_name):
        calls.append((appid, api_name))
        return True, ""
    return f


def _rejecting_fire(calls: list) -> callable:
    def f(appid, api_name):
        calls.append((appid, api_name))
        return False, "server rejected"
    return f


def test_init_db_creates_schema():
    conn = _mem_db()
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables >= {"campaigns", "unlocks", "unverifiable_games"}


def test_add_campaign_populates_tables(tmp_path=Path("/tmp/test_add")):
    tmp_path.mkdir(exist_ok=True)
    schedule = _make_schedule_file(
        tmp_path,
        appid=42,
        unlocks=[_unlock("ACH1", "2026-04-10T00:00:00+00:00")],
    )
    conn = _mem_db()
    orch = Orchestrator(conn, _fake_fire([]), FakeSteam())
    campaign_id = orch.add_campaign(schedule)
    assert campaign_id == 1
    row = conn.execute("SELECT appid, target_hours FROM campaigns WHERE id = ?",
                       (campaign_id,)).fetchone()
    assert row == (42, 10.0)
    unlocks = conn.execute(
        "SELECT api_name, state FROM unlocks WHERE campaign_id = ?",
        (campaign_id,),
    ).fetchall()
    assert unlocks == [("ACH1", "pending")]


def test_tick_fires_due_unlocks(tmp_path=Path("/tmp/test_fire")):
    tmp_path.mkdir(exist_ok=True)
    schedule = _make_schedule_file(
        tmp_path,
        appid=42,
        unlocks=[
            _unlock("EARLY", "2026-04-09T00:00:00+00:00"),  # already due
            _unlock("LATE", "2099-01-01T00:00:00+00:00"),  # far future
        ],
    )
    conn = _mem_db()
    steam = FakeSteam()
    steam.state_by_appid[42] = SteamState(playtime_hours=5.0, unlocked=set())
    calls: list = []
    orch = Orchestrator(
        conn, _fake_fire(calls), steam,
        now_func=lambda: datetime(2026, 4, 10, 12, 0, tzinfo=timezone.utc),
    )
    orch.add_campaign(schedule)
    result = orch.tick()
    assert result.fired == 1
    assert calls == [(42, "EARLY")]
    states = dict(conn.execute(
        "SELECT api_name, state FROM unlocks WHERE campaign_id = 1"
    ).fetchall())
    assert states == {"EARLY": "fired", "LATE": "pending"}


def test_tick_holds_unlocks_whose_playtime_gate_not_met(tmp_path=Path("/tmp/test_hold")):
    tmp_path.mkdir(exist_ok=True)
    schedule = _make_schedule_file(
        tmp_path,
        appid=42,
        unlocks=[_unlock("GATED", "2026-04-09T00:00:00+00:00", time_requirement_hours=50.0)],
    )
    conn = _mem_db()
    steam = FakeSteam()
    steam.state_by_appid[42] = SteamState(playtime_hours=5.0, unlocked=set())  # 5 < 50
    calls: list = []
    orch = Orchestrator(
        conn, _fake_fire(calls), steam,
        now_func=lambda: datetime(2026, 4, 10, tzinfo=timezone.utc),
    )
    orch.add_campaign(schedule)
    res = orch.tick()
    assert res.fired == 0
    assert res.skipped == 1
    assert calls == []
    (st,) = conn.execute("SELECT state FROM unlocks WHERE api_name = 'GATED'").fetchone()
    assert st == "pending"


def test_tick_verifies_unlock_when_steam_reports_it(tmp_path=Path("/tmp/test_verify")):
    tmp_path.mkdir(exist_ok=True)
    schedule = _make_schedule_file(
        tmp_path,
        appid=42,
        unlocks=[_unlock("ACH1", "2026-04-09T00:00:00+00:00")],
    )
    conn = _mem_db()
    steam = FakeSteam()
    steam.state_by_appid[42] = SteamState(playtime_hours=5.0, unlocked={"ACH1"})
    calls: list = []
    orch = Orchestrator(
        conn, _fake_fire(calls), steam,
        now_func=lambda: datetime(2026, 4, 10, tzinfo=timezone.utc),
    )
    orch.add_campaign(schedule)
    res = orch.tick()
    assert res.verified == 1
    assert res.fired == 0  # verified before the fire step
    assert calls == []


def test_silent_failure_marks_campaign_invalid(tmp_path=Path("/tmp/test_silent")):
    tmp_path.mkdir(exist_ok=True)
    schedule = _make_schedule_file(
        tmp_path,
        appid=42,
        unlocks=[_unlock("ACH1", "2026-04-09T00:00:00+00:00")],
    )
    conn = _mem_db()
    steam = FakeSteam()
    steam.state_by_appid[42] = SteamState(playtime_hours=5.0, unlocked=set())
    calls: list = []
    orch = Orchestrator(
        conn, _fake_fire(calls), steam,
        now_func=lambda: datetime(2026, 4, 10, tzinfo=timezone.utc),
    )
    orch.add_campaign(schedule)
    for _ in range(MAX_VERIFY_ATTEMPTS + 1):
        orch.tick()
    (state,) = conn.execute("SELECT state FROM campaigns WHERE id = 1").fetchone()
    assert state == "invalid"
    (unlock_state,) = conn.execute(
        "SELECT state FROM unlocks WHERE api_name = 'ACH1'"
    ).fetchone()
    assert unlock_state == "failed"
    # appid should now be in unverifiable list
    row = conn.execute("SELECT 1 FROM unverifiable_games WHERE appid = 42").fetchone()
    assert row is not None


def test_unverifiable_games_not_fired_again(tmp_path=Path("/tmp/test_unverifiable")):
    tmp_path.mkdir(exist_ok=True)
    conn = _mem_db()
    conn.execute(
        "INSERT INTO unverifiable_games (appid, first_detected_at) VALUES (?, ?)",
        (42, "2026-04-10T00:00:00+00:00"),
    )
    conn.commit()
    schedule = _make_schedule_file(
        tmp_path,
        appid=42,
        unlocks=[_unlock("ACH1", "2026-04-09T00:00:00+00:00")],
    )
    steam = FakeSteam()
    steam.state_by_appid[42] = SteamState(playtime_hours=5.0, unlocked=set())
    calls: list = []
    orch = Orchestrator(
        conn, _fake_fire(calls), steam,
        now_func=lambda: datetime(2026, 4, 10, tzinfo=timezone.utc),
    )
    orch.add_campaign(schedule)
    orch.tick()
    assert calls == []  # never fired on known-bad game


def test_tick_completes_campaign_when_all_verified(tmp_path=Path("/tmp/test_complete")):
    tmp_path.mkdir(exist_ok=True)
    schedule = _make_schedule_file(
        tmp_path,
        appid=42,
        unlocks=[_unlock("A", "2026-04-09T00:00:00+00:00")],
    )
    conn = _mem_db()
    steam = FakeSteam()
    steam.state_by_appid[42] = SteamState(playtime_hours=5.0, unlocked=set())
    orch = Orchestrator(
        conn, _fake_fire([]), steam,
        now_func=lambda: datetime(2026, 4, 10, tzinfo=timezone.utc),
    )
    orch.add_campaign(schedule)
    orch.tick()  # fires
    # Now the fake Steam reports it as unlocked
    steam.state_by_appid[42] = SteamState(playtime_hours=5.0, unlocked={"A"})
    orch.tick()  # verifies and completes
    (state,) = conn.execute("SELECT state FROM campaigns WHERE id = 1").fetchone()
    assert state == "completed"


def test_fire_failure_recorded_as_last_error(tmp_path=Path("/tmp/test_fire_fail")):
    tmp_path.mkdir(exist_ok=True)
    schedule = _make_schedule_file(
        tmp_path,
        appid=42,
        unlocks=[_unlock("ACH1", "2026-04-09T00:00:00+00:00")],
    )
    conn = _mem_db()
    steam = FakeSteam()
    steam.state_by_appid[42] = SteamState(playtime_hours=5.0, unlocked=set())
    calls: list = []
    orch = Orchestrator(
        conn, _rejecting_fire(calls), steam,
        now_func=lambda: datetime(2026, 4, 10, tzinfo=timezone.utc),
    )
    orch.add_campaign(schedule)
    res = orch.tick()
    assert res.errors == 1
    assert res.fired == 0
    row = conn.execute("SELECT state, last_error FROM unlocks WHERE api_name = 'ACH1'").fetchone()
    assert row[0] == "pending"  # still pending, retried next tick
    assert "server rejected" in (row[1] or "")


def test_tick_does_not_refire_already_fired(tmp_path=Path("/tmp/test_norefire")):
    """After firing once, the local 'fired' state prevents re-firing during the API cache window."""
    tmp_path.mkdir(exist_ok=True)
    schedule = _make_schedule_file(
        tmp_path,
        appid=42,
        unlocks=[_unlock("ACH1", "2026-04-09T00:00:00+00:00")],
    )
    conn = _mem_db()
    steam = FakeSteam()
    steam.state_by_appid[42] = SteamState(playtime_hours=5.0, unlocked=set())
    calls: list = []
    orch = Orchestrator(
        conn, _fake_fire(calls), steam,
        now_func=lambda: datetime(2026, 4, 10, tzinfo=timezone.utc),
    )
    orch.add_campaign(schedule)
    orch.tick()  # fires once
    assert len(calls) == 1
    # Steam API still hasn't caught up; next tick should NOT re-fire
    orch.tick()
    orch.tick()
    assert len(calls) == 1


def test_subprocess_fire_success():
    fire = make_subprocess_fire(Path("/bin/true"))
    ok, err = fire(42, "ACH")
    assert ok
    assert err == ""


def test_subprocess_fire_nonzero_captures_exit_code():
    # /bin/false ignores args, exits 1, no stderr -> falls back to "exit N"
    fire = make_subprocess_fire(Path("/bin/false"))
    ok, err = fire(42, "ACH")
    assert not ok
    assert "exit 1" in err


def test_subprocess_fire_missing_binary():
    fire = make_subprocess_fire(Path("/does/not/exist"))
    ok, err = fire(42, "ACH")
    assert not ok
    assert err.startswith("subprocess:")


def test_paused_campaign_is_not_ticked(tmp_path=Path("/tmp/test_paused")):
    tmp_path.mkdir(exist_ok=True)
    schedule = _make_schedule_file(
        tmp_path,
        appid=42,
        unlocks=[_unlock("ACH1", "2026-04-09T00:00:00+00:00")],
    )
    conn = _mem_db()
    steam = FakeSteam()
    steam.state_by_appid[42] = SteamState(playtime_hours=5.0, unlocked=set())
    calls: list = []
    orch = Orchestrator(
        conn, _fake_fire(calls), steam,
        now_func=lambda: datetime(2026, 4, 10, tzinfo=timezone.utc),
    )
    orch.add_campaign(schedule)
    conn.execute("UPDATE campaigns SET state = 'paused' WHERE id = 1")
    conn.commit()
    orch.tick()
    assert calls == []


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
