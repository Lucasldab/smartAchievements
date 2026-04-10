import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

STEAM_API = "https://api.steampowered.com"


def fetch_unlocked(appid: int, steamid: str, api_key: str) -> set[str]:
    url = f"{STEAM_API}/ISteamUserStats/GetUserStatsForGame/v2/?" + urllib.parse.urlencode({
        "key": api_key,
        "steamid": steamid,
        "appid": appid,
    })
    with urllib.request.urlopen(url, timeout=10) as r:
        data = json.loads(r.read())
    ach = data.get("playerstats", {}).get("achievements", [])
    return {a["name"] for a in ach if a.get("achieved") == 1}


def fetch_playtime_hours(appid: int, steamid: str, api_key: str) -> float:
    url = f"{STEAM_API}/IPlayerService/GetOwnedGames/v1/?" + urllib.parse.urlencode({
        "key": api_key,
        "steamid": steamid,
        "include_played_free_games": 1,
    })
    with urllib.request.urlopen(url, timeout=15) as r:
        data = json.loads(r.read())
    for g in data.get("response", {}).get("games", []):
        if g.get("appid") == appid:
            return float(g.get("playtime_forever", 0)) / 60.0
    return 0.0


def run_once(schedule_path: Path, unlocker_path: Path) -> int:
    schedule = json.loads(schedule_path.read_text())
    appid = int(schedule["appid"])

    api_key = os.environ.get("STEAM_WEB_API_KEY") or os.environ.get("STEAM_API_KEY")
    steamid = os.environ.get("STEAM_ID")
    if not (api_key and steamid):
        print("STEAM_API_KEY and STEAM_ID are required", file=sys.stderr)
        return 1

    try:
        unlocked = fetch_unlocked(appid, steamid, api_key)
        playtime = fetch_playtime_hours(appid, steamid, api_key)
    except Exception as e:
        print(f"fetch state failed: {e}", file=sys.stderr)
        return 1

    now = datetime.now(timezone.utc)
    fired = 0
    pending = 0

    for entry in schedule["unlocks"]:
        api_name = entry["api_name"]
        if api_name in unlocked:
            continue
        pending += 1

        unlock_at = datetime.fromisoformat(entry["unlock_at"])
        if unlock_at > now:
            continue

        time_req = entry.get("time_requirement_hours")
        if time_req is not None and playtime < time_req:
            print(
                f"[{now.isoformat(timespec='seconds')}] skip {api_name}: "
                f"playtime {playtime:.1f}h < required {time_req}h",
                file=sys.stderr,
            )
            continue

        print(
            f"[{now.isoformat(timespec='seconds')}] firing {api_name} "
            f"({entry.get('display_name', '')})",
            file=sys.stderr,
        )
        result = subprocess.run(
            [str(unlocker_path), "--appid", str(appid), "--achievement", api_name],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(f"  FAILED: {result.stderr.strip()}", file=sys.stderr)
            return 1
        print(f"  OK: {result.stdout.strip()}", file=sys.stderr)
        unlocked.add(api_name)
        fired += 1

    remaining = pending - fired
    print(
        f"[{now.isoformat(timespec='seconds')}] fired={fired} pending={remaining}",
        file=sys.stderr,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("schedule", type=Path)
    ap.add_argument("--unlocker", type=Path, default=None)
    args = ap.parse_args(argv)

    unlocker_path = args.unlocker or (
        Path(__file__).resolve().parent / "unlocker" / "target" / "release" / "unlocker"
    )
    if not unlocker_path.exists():
        print(f"unlocker binary not found: {unlocker_path}", file=sys.stderr)
        return 1

    return run_once(args.schedule, unlocker_path)


if __name__ == "__main__":
    sys.exit(main())
