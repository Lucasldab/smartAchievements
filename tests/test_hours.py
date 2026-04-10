import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hours import (
    HoursEstimate,
    load_cache,
    parse_hltb_hours,
    rarity_heuristic_hours,
    resolve_hours,
    save_cache,
)


HLTB_FIXTURE = '''<html><head><script id="__NEXT_DATA__">{
"pageProps":{"game":{"game_name":"Some Game","comp_main":2808,"comp_main_med":2700,
"comp_plus":6967,"comp_plus_med":6450,"comp_100":8846,"comp_100_med":7200,
"comp_all":4550,"comp_all_med":3600}}}</script></head></html>'''


def test_parse_hltb_prefers_comp_100_med():
    hours = parse_hltb_hours(HLTB_FIXTURE)
    assert abs(hours - 2.0) < 1e-9  # 7200 seconds = 2h


def test_parse_hltb_falls_back_when_comp_100_missing():
    html = '"comp_all_med":3600,"comp_all":4550'
    hours = parse_hltb_hours(html)
    assert abs(hours - 1.0) < 1e-9  # 3600 seconds = 1h


def test_parse_hltb_skips_zero_fields():
    html = '"comp_100_med":0,"comp_100_avg":0,"comp_100":0,"comp_all_med":7200'
    hours = parse_hltb_hours(html)
    assert abs(hours - 2.0) < 1e-9


def test_parse_hltb_raises_when_nothing_found():
    try:
        parse_hltb_hours('<html>no data here</html>')
    except ValueError:
        return
    raise AssertionError("expected ValueError")


def test_rarity_heuristic_grindier_tail_gives_more_hours():
    easy = [70.0] * 20
    grindy = [70.0] * 15 + [3.0] * 5  # rare tail is 25% of the distribution
    assert rarity_heuristic_hours(grindy) > rarity_heuristic_hours(easy)


def test_rarity_heuristic_robust_to_single_outlier():
    mostly_easy = [70.0] * 19 + [1.0]
    all_easy = [70.0] * 20
    # one rare achievement should not dramatically shift the estimate
    assert abs(rarity_heuristic_hours(mostly_easy) - rarity_heuristic_hours(all_easy)) < 0.5


def test_rarity_heuristic_more_achievements_gives_more_hours():
    small = [20.0] * 10
    large = [20.0] * 100
    assert rarity_heuristic_hours(large) > rarity_heuristic_hours(small)


def test_rarity_heuristic_handles_empty():
    assert rarity_heuristic_hours([]) == 10.0


def test_cache_round_trip(tmp_path=None):
    from tempfile import TemporaryDirectory
    with TemporaryDirectory() as td:
        path = Path(td) / "hours.json"
        save_cache({"413150": {"hours": 52.0, "source": "manual"}}, path)
        loaded = load_cache(path)
        assert loaded["413150"]["hours"] == 52.0
        assert loaded["413150"]["source"] == "manual"


def test_load_cache_missing_returns_empty():
    assert load_cache(Path("/nonexistent/path/hours.json")) == {}


def test_resolve_manual_override_writes_cache():
    from tempfile import TemporaryDirectory
    with TemporaryDirectory() as td:
        cache_path = Path(td) / "hours.json"
        est = resolve_hours(413150, [50.0, 10.0], manual=40.0, cache_path=cache_path)
        assert est.hours == 40.0
        assert est.source == "manual"
        reloaded = load_cache(cache_path)
        assert reloaded["413150"]["hours"] == 40.0


def test_resolve_cache_hit_returns_stored():
    from tempfile import TemporaryDirectory
    with TemporaryDirectory() as td:
        cache_path = Path(td) / "hours.json"
        save_cache({"100": {"hours": 25.5, "source": "hltb:123"}}, cache_path)
        est = resolve_hours(100, [50.0], cache_path=cache_path)
        assert est.hours == 25.5
        assert est.source == "cached:hltb:123"


def test_resolve_falls_back_to_heuristic_when_no_cache_no_id():
    from tempfile import TemporaryDirectory
    with TemporaryDirectory() as td:
        cache_path = Path(td) / "hours.json"
        est = resolve_hours(999, [50.0, 30.0, 10.0, 5.0, 2.0], cache_path=cache_path)
        assert est.source == "heuristic"
        assert est.hours > 0


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
