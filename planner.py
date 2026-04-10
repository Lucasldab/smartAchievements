import argparse
import json
import os
import random
import re
import sys
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

from hours import HoursEstimate, resolve_hours

STEAM_API = "https://api.steampowered.com"

# the "for" bridge rejects speedrun phrasing like "win in under 5 hours".
_TIME_RE = re.compile(
    r"\b(?:play|spend|farm|idle|be|stay|remain|survive|last)\b"
    r"[^.]*?\bfor\b[^.]{0,30}?"
    r"(\d+(?:\.\d+)?)\s*"
    r"(hours?|hrs?|minutes?|mins?|days?|seconds?|secs?)\b",
    re.IGNORECASE,
)

_UNIT_HOURS = {
    "hour": 1.0, "hours": 1.0, "hr": 1.0, "hrs": 1.0,
    "minute": 1 / 60, "minutes": 1 / 60, "min": 1 / 60, "mins": 1 / 60,
    "day": 24.0, "days": 24.0,
    "second": 1 / 3600, "seconds": 1 / 3600, "sec": 1 / 3600, "secs": 1 / 3600,
}


@dataclass
class Achievement:
    api_name: str
    display_name: str
    global_percent: float
    time_requirement_hours: float | None = None


@dataclass
class PlannedUnlock:
    api_name: str
    display_name: str
    global_percent: float
    in_game_hour: float
    unlock_at: str
    time_requirement_hours: float | None = None


def detect_time_requirement_hours(description: str) -> float | None:
    if not description:
        return None
    m = _TIME_RE.search(description)
    if not m:
        return None
    return float(m.group(1)) * _UNIT_HOURS[m.group(2).lower()]


def fetch_schema(appid: int, api_key: str) -> dict[str, dict[str, str]]:
    url = f"{STEAM_API}/ISteamUserStats/GetSchemaForGame/v2/"
    qs = urllib.parse.urlencode({"key": api_key, "appid": appid})
    with urllib.request.urlopen(f"{url}?{qs}") as r:
        data = json.loads(r.read())
    rows = (
        data.get("game", {})
        .get("availableGameStats", {})
        .get("achievements", [])
    )
    return {
        row["name"]: {
            "display_name": row.get("displayName", row["name"]),
            "description": row.get("description", ""),
        }
        for row in rows
    }


def fetch_global_rarity(appid: int) -> dict[str, float]:
    url = f"{STEAM_API}/ISteamUserStats/GetGlobalAchievementPercentagesForApp/v2/"
    qs = urllib.parse.urlencode({"gameid": appid})
    with urllib.request.urlopen(f"{url}?{qs}") as r:
        data = json.loads(r.read())
    rows = data.get("achievementpercentages", {}).get("achievements", [])
    return {row["name"]: float(row["percent"]) for row in rows}


def load_achievements(appid: int, api_key: str | None) -> list[Achievement]:
    rarity = fetch_global_rarity(appid)
    schema: dict[str, dict[str, str]] = fetch_schema(appid, api_key) if api_key else {}
    out: list[Achievement] = []
    for api_name, pct in rarity.items():
        entry = schema.get(api_name, {})
        display_name = entry.get("display_name", api_name)
        description = entry.get("description", "")
        out.append(
            Achievement(
                api_name=api_name,
                display_name=display_name,
                global_percent=pct,
                time_requirement_hours=detect_time_requirement_hours(description),
            )
        )
    return out


def natural_positions(
    achievements: list[Achievement],
    jitter_sigma: float,
    rng: random.Random,
) -> list[float]:
    out: list[float] = []
    for ach in achievements:
        base = 1.0 - (ach.global_percent / 100.0)
        jittered = base + rng.gauss(0.0, jitter_sigma)
        # Reflect at the [0,1] boundary so clusters near 1.0 don't all
        # hard-clamp and resolve to the same timestamp.
        if jittered < 0.0:
            jittered = -jittered
        if jittered > 1.0:
            jittered = 2.0 - jittered
        out.append(max(0.0, min(1.0, jittered)))
    return out


def build_sessions(
    total_hours: float,
    start: datetime,
    rng: random.Random,
) -> list[tuple[datetime, datetime]]:
    sessions: list[tuple[datetime, datetime]] = []
    cursor = start
    played = 0.0
    while played < total_hours:
        length_h = min(rng.uniform(2.0, 4.0), total_hours - played)
        end = cursor + timedelta(hours=length_h)
        sessions.append((cursor, end))
        played += length_h
        if played < total_hours:
            cursor = end + timedelta(hours=rng.uniform(12.0, 24.0))
    return sessions


def project_to_calendar(
    in_game_hour: float,
    sessions: list[tuple[datetime, datetime]],
) -> datetime:
    remaining = in_game_hour
    for s_start, s_end in sessions:
        length_h = (s_end - s_start).total_seconds() / 3600.0
        if remaining <= length_h:
            return s_start + timedelta(hours=remaining)
        remaining -= length_h
    return sessions[-1][1]  # fallback for floating-point overshoot


def plan_campaign(
    achievements: list[Achievement],
    target_hours: float,
    start: datetime,
    seed: int | None = None,
    jitter_sigma: float = 0.05,
) -> list[PlannedUnlock]:
    rng = random.Random(seed)
    sessions = build_sessions(target_hours, start, rng)

    unlocks: list[PlannedUnlock] = []
    for ach in achievements:
        if ach.time_requirement_hours is not None:
            if ach.time_requirement_hours > target_hours:
                continue
            in_game_hour = ach.time_requirement_hours
        else:
            base = 1.0 - (ach.global_percent / 100.0)
            jittered = base + rng.gauss(0.0, jitter_sigma)
            if jittered < 0.0:
                jittered = -jittered
            if jittered > 1.0:
                jittered = 2.0 - jittered
            pos = max(0.0, min(1.0, jittered))
            in_game_hour = pos * target_hours

        unlocks.append(
            PlannedUnlock(
                api_name=ach.api_name,
                display_name=ach.display_name,
                global_percent=ach.global_percent,
                in_game_hour=round(in_game_hour, 3),
                unlock_at=project_to_calendar(in_game_hour, sessions).isoformat(),
                time_requirement_hours=ach.time_requirement_hours,
            )
        )

    unlocks.sort(key=lambda u: u.unlock_at)
    return unlocks


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--appid", type=int, required=True)
    ap.add_argument("--hours", type=float, default=None)
    ap.add_argument("--hltb-id", type=int, default=None)
    ap.add_argument("--refresh-hours", action="store_true")
    ap.add_argument("--start", type=str, default=None)
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--jitter", type=float, default=0.05)
    ap.add_argument("--out", type=str, default="-")
    args = ap.parse_args(argv)

    start = datetime.fromisoformat(args.start) if args.start else datetime.now(timezone.utc)
    api_key = os.environ.get("STEAM_WEB_API_KEY") or os.environ.get("STEAM_API_KEY")
    achievements = load_achievements(args.appid, api_key)
    if not achievements:
        print(f"no achievements found for appid {args.appid}", file=sys.stderr)
        return 1

    estimate = resolve_hours(
        args.appid,
        [a.global_percent for a in achievements],
        hltb_id=args.hltb_id,
        manual=args.hours,
        refresh=args.refresh_hours,
    )

    if args.hours is None:
        time_gates = [a.time_requirement_hours for a in achievements if a.time_requirement_hours is not None]
        if time_gates:
            max_tr = max(time_gates)
            if max_tr > estimate.hours:
                print(
                    f"auto-extending hours from {estimate.hours:.1f} to {max_tr:.1f} (longest time-gate)",
                    file=sys.stderr,
                )
                estimate = HoursEstimate(hours=max_tr, source=f"{estimate.source}+time-gate")

    over_budget = [
        a for a in achievements
        if a.time_requirement_hours is not None and a.time_requirement_hours > estimate.hours
    ]
    if over_budget:
        print(
            f"warning: {len(over_budget)} time-gated achievements exceed --hours {estimate.hours:.1f}; excluding them",
            file=sys.stderr,
        )
        for a in over_budget:
            print(f"  - {a.api_name} ({a.display_name}) requires {a.time_requirement_hours:.1f}h", file=sys.stderr)

    print(f"hours: {estimate.hours:.1f} (source: {estimate.source})", file=sys.stderr)

    unlocks = plan_campaign(
        achievements,
        target_hours=estimate.hours,
        start=start,
        seed=args.seed,
        jitter_sigma=args.jitter,
    )
    payload = {
        "appid": args.appid,
        "target_hours": estimate.hours,
        "hours_source": estimate.source,
        "planned_start": start.isoformat(),
        "seed": args.seed,
        "jitter_sigma": args.jitter,
        "unlocks": [asdict(u) for u in unlocks],
    }
    text = json.dumps(payload, indent=2)
    if args.out == "-":
        print(text)
    else:
        with open(args.out, "w") as f:
            f.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
