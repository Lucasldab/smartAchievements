# smartAchievements

ASF + Steam-Achievement-Manager-style orchestrator for a personal Steam
account. Given an appid, it generates a session-aware unlock schedule and
drips achievements through a small Rust binary over calendar time, so the
resulting profile looks like an organic playthrough instead of a
synchronous bulk unlock.

Personal use only. Single account. Not for distribution. Steam's TOS is
known-hostile to this; nothing in the repo tries to defeat server-side
validation, and games that silently reject `SetAchievement` get caught by
the orchestrator's `unverifiable_games` table after a few failed verifies
and are then skipped.

## Pieces

`planner.py` fetches the achievement schema and global rarity from Steam,
detects time-gated achievements from their descriptions, reads current
playtime on the target game via `GetOwnedGames`, filters already-unlocked
entries via `GetUserStatsForGame`, and emits a JSON schedule with
per-unlock calendar timestamps. Rarity-based achievements are positioned
at `1 - rarity/100` of the target window with Gaussian jitter and
boundary reflection; time-gated ones fire at their exact cumulative
playtime threshold.

`hours.py` resolves the target hours from, in order: an explicit `--hours`
override, a persistent cache at `~/.cache/smartachievements/hours.json`,
an HLTB scrape via `--hltb-id` (prefers the slowest `comp_*_h` fields so
the calendar has generous headroom), or a rarity-distribution heuristic as
last resort. If any time-gated achievement exceeds the resolved target,
the window auto-extends to cover the longest gate.

`unlocker/` is a self-contained Rust binary around `steamworks-rs`. Takes
`--appid` and `--achievement`, initializes Steamworks against the game,
calls `SetAchievement` + `StoreStats`, and exits. A `--dry-run` flag
prints the intended action without touching Steam. `build.rs` copies
`libsteam_api.so` next to the binary on every build and
`.cargo/config.toml` sets `$ORIGIN` rpath so the binary is portable.

`orchestrator.py` is a SQLite-backed state machine that manages multiple
campaigns concurrently. Each tick: fetches current Steam state, verifies
fired unlocks (unioning `GetPlayerAchievements` and `GetUserStatsForGame`
because both cache independently), fires due pending unlocks whose
playtime gate is met, bumps verify attempts on still-fired unlocks, and
marks a game as unverifiable after the verify threshold is exceeded.
Local state tracking prevents re-firing an unlock while Valve's Web API
cache catches up.

## Prerequisites

Python 3.11+, cargo, a running Steam client logged in, ArchiSteamFarm
running for any game whose time-gated achievements need accumulated
playtime, and the env vars `STEAM_API_KEY` (or `STEAM_WEB_API_KEY`) and
`STEAM_ID` exported.

## Build

    cd unlocker && cargo build --release

## Install the systemd user timer

    cp systemd/smartachievements.service ~/.config/systemd/user/
    cp systemd/smartachievements.timer ~/.config/systemd/user/
    systemctl --user daemon-reload
    systemctl --user enable --now smartachievements.timer

After install, ticks run every minute automatically.

## Per-game workflow

    python3 planner.py --appid 3880190 --out /tmp/c.json
    python3 orchestrator.py add /tmp/c.json
    python3 orchestrator.py list

Add `--hltb-id <id>` to the planner invocation for a more accurate target
than the rarity heuristic. Add `--limit N` to cap the schedule to N
achievements spread evenly across the rarity distribution (useful for
compressed test runs).

Monitor with `orchestrator.py list`, `orchestrator.py status <id>`, and
`journalctl --user -u smartachievements.service`. Control with
`orchestrator.py pause|resume|remove <id>`. Run a manual tick without
waiting for the timer with `orchestrator.py tick`.

## Tests

    python3 tests/test_planner.py
    python3 tests/test_hours.py
    python3 tests/test_orchestrator.py

45 tests across the three suites; no network required.

## Known limits

HLTB IDs are manual per game because their search endpoint rotates a key
and stdlib scraping can't follow it reliably; the game page at
`howlongtobeat.com/game/<id>` is scraped once you've looked the ID up in
a browser and cached forever after.

Valve's Web API caches both achievement endpoints independently with
unpredictable refresh intervals. The orchestrator unions both and relies
on its own local fired-state tracking rather than trusting the API.

Games with server-side validation silently accept `SetAchievement` and
drop the write. The `unverifiable_games` table catches these after
`MAX_VERIFY_ATTEMPTS` and the affected campaign is marked invalid. There
is no programmatic pre-detection — you find out by trying.

No ASF IPC integration. The orchestrator observes playtime through the
Web API and trusts you to manage ArchiSteamFarm separately.
