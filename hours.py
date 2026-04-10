import json
import math
import re
import sys
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

CACHE_PATH = Path.home() / ".cache" / "smartachievements" / "hours.json"
HLTB_GAME_URL = "https://howlongtobeat.com/game/{id}"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64; rv:122.0) Gecko/20100101 Firefox/122.0"

# median before mean before legacy; full-completion fields before any-style.
_HLTB_FIELDS = (
    "comp_100_med",
    "comp_100_avg",
    "comp_100",
    "comp_all_med",
    "comp_all_avg",
    "comp_all",
)


@dataclass
class HoursEstimate:
    hours: float
    source: str


def load_cache(path: Path = CACHE_PATH) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def save_cache(cache: dict, path: Path = CACHE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, indent=2))


def parse_hltb_hours(html: str) -> float:
    for field in _HLTB_FIELDS:
        m = re.search(rf'"{field}":(\d+)', html)
        if m:
            seconds = int(m.group(1))
            if seconds > 0:
                return seconds / 3600.0
    raise ValueError("no completion time field found in HLTB page")


def fetch_hltb_hours(hltb_id: int) -> float:
    req = urllib.request.Request(
        HLTB_GAME_URL.format(id=hltb_id),
        headers={"User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        html = r.read().decode("utf-8", errors="replace")
    return parse_hltb_hours(html)


def rarity_heuristic_hours(rarities: list[float]) -> float:
    if not rarities:
        return 10.0
    n = len(rarities)
    sorted_r = sorted(rarities)
    # use the ~10th percentile as a tail proxy; floor at 0.5% to avoid blowups
    tail = max(sorted_r[max(0, n // 10)], 0.5)
    return round((4.0 + 0.5 * n ** 0.5) * math.log(100.0 / tail), 1)


def _write_cache(cache: dict, key: str, est: HoursEstimate, path: Path, extra: dict | None = None) -> None:
    cache[key] = {
        "hours": est.hours,
        "source": est.source,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        **(extra or {}),
    }
    save_cache(cache, path)


def resolve_hours(
    appid: int,
    rarities: list[float],
    hltb_id: int | None = None,
    manual: float | None = None,
    refresh: bool = False,
    cache_path: Path = CACHE_PATH,
) -> HoursEstimate:
    cache = load_cache(cache_path)
    key = str(appid)

    if manual is not None:
        est = HoursEstimate(hours=float(manual), source="manual")
        _write_cache(cache, key, est, cache_path)
        return est

    if not refresh and key in cache:
        entry = cache[key]
        return HoursEstimate(hours=float(entry["hours"]), source=f"cached:{entry['source']}")

    if hltb_id is not None:
        try:
            hours = fetch_hltb_hours(hltb_id)
            est = HoursEstimate(hours=hours, source=f"hltb:{hltb_id}")
            _write_cache(cache, key, est, cache_path, extra={"hltb_id": hltb_id})
            return est
        except Exception as e:
            print(f"hltb fetch failed ({e}); falling back to heuristic", file=sys.stderr)

    hours = rarity_heuristic_hours(rarities)
    return HoursEstimate(hours=hours, source="heuristic")
