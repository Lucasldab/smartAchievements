import argparse
import json
import os
import random
import sys
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

from hours import resolve_hours

STEAM_API = "https://api.steampowered.com"


@dataclass
class Achievement:
    api_name: str
    display_name: str
    global_percent: float


@dataclass
class PlannedUnlock:
    api_name: str
    display_name: str
    global_percent: float
    in_game_hour: float
    unlock_at: str


def fetch_schema(appid: int, api_key: str) -> dict[str, str]:
    url = f"{STEAM_API}/ISteamUserStats/GetSchemaForGame/v2/"
    qs = urllib.parse.urlencode({"key": api_key, "appid": appid})
    with urllib.request.urlopen(f"{url}?{qs}") as r:
        data = json.loads(r.read())
    rows = (
        data.get("game", {})
        .get("availableGameStats", {})
        .get("achievements", [])
    )
    return {row["name"]: row.get("displayName", row["name"]) for row in rows}


def fetch_global_rarity(appid: int) -> dict[str, float]:
    url = f"{STEAM_API}/ISteamUserStats/GetGlobalAchievementPercentagesForApp/v2/"
    qs = urllib.parse.urlencode({"gameid": appid})
    with urllib.request.urlopen(f"{url}?{qs}") as r:
        data = json.loads(r.read())
    rows = data.get("achievementpercentages", {}).get("achievements", [])
    return {row["name"]: float(row["percent"]) for row in rows}


def load_achievements(appid: int, api_key: str | None) -> list[Achievement]:
    rarity = fetch_global_rarity(appid)
    names: dict[str, str] = fetch_schema(appid, api_key) if api_key else {}
    return [
        Achievement(
            api_name=api_name,
            display_name=names.get(api_name, api_name),
            global_percent=pct,
        )
        for api_name, pct in rarity.items()
    ]


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
    positions = natural_positions(achievements, jitter_sigma, rng)
    sessions = build_sessions(target_hours, start, rng)
    unlocks = [
        PlannedUnlock(
            api_name=ach.api_name,
            display_name=ach.display_name,
            global_percent=ach.global_percent,
            in_game_hour=round(pos * target_hours, 3),
            unlock_at=project_to_calendar(pos * target_hours, sessions).isoformat(),
        )
        for ach, pos in zip(achievements, positions)
    ]
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
