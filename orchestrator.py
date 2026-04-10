import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

STEAM_API = "https://api.steampowered.com"
DEFAULT_DB = (
    Path(os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")))
    / "smartachievements"
    / "campaigns.db"
)
DEFAULT_UNLOCKER = Path(__file__).resolve().parent / "unlocker" / "target" / "release" / "unlocker"
MAX_VERIFY_ATTEMPTS = 20

SCHEMA = """
CREATE TABLE IF NOT EXISTS campaigns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    appid INTEGER NOT NULL,
    schedule_path TEXT,
    created_at TEXT NOT NULL,
    target_hours REAL NOT NULL,
    hours_source TEXT,
    current_playtime_at_plan REAL NOT NULL DEFAULT 0.0,
    seed INTEGER,
    jitter_sigma REAL,
    state TEXT NOT NULL DEFAULT 'active',
    completed_at TEXT,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS unlocks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id INTEGER NOT NULL,
    api_name TEXT NOT NULL,
    display_name TEXT,
    global_percent REAL,
    in_game_hour REAL NOT NULL,
    time_requirement_hours REAL,
    unlock_at TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    fired_at TEXT,
    verified_at TEXT,
    verify_attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    FOREIGN KEY (campaign_id) REFERENCES campaigns(id) ON DELETE CASCADE,
    UNIQUE (campaign_id, api_name)
);

CREATE INDEX IF NOT EXISTS idx_unlocks_campaign ON unlocks(campaign_id);
CREATE INDEX IF NOT EXISTS idx_unlocks_state ON unlocks(state);

CREATE TABLE IF NOT EXISTS unverifiable_games (
    appid INTEGER PRIMARY KEY,
    first_detected_at TEXT NOT NULL,
    reason TEXT
);
"""


def init_db(db_path: Path | str) -> sqlite3.Connection:
    if isinstance(db_path, Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


@dataclass
class SteamState:
    playtime_hours: float
    unlocked: set[str] = field(default_factory=set)


class SteamClient:
    def __init__(self, api_key: str, steamid: str):
        self.api_key = api_key
        self.steamid = steamid

    def get_state(self, appid: int) -> SteamState:
        return SteamState(
            playtime_hours=self._get_playtime(appid),
            unlocked=self._get_unlocked(appid),
        )

    def _get_playtime(self, appid: int) -> float:
        url = f"{STEAM_API}/IPlayerService/GetOwnedGames/v1/?" + urllib.parse.urlencode({
            "key": self.api_key,
            "steamid": self.steamid,
            "include_played_free_games": 1,
        })
        with urllib.request.urlopen(url, timeout=15) as r:
            data = json.loads(r.read())
        for g in data.get("response", {}).get("games", []):
            if g.get("appid") == appid:
                return float(g.get("playtime_forever", 0)) / 60.0
        return 0.0

    def _get_unlocked(self, appid: int) -> set[str]:
        # Query both endpoints and union: they cache independently, so either
        # one may lag on a fresh write. If BOTH endpoints fail we raise so the
        # tick counts it as an error rather than silently verifying nothing
        # and draining verify_attempts toward the unverifiable terminal.
        names: set[str] = set()
        errors: list[str] = []
        urls = (self._url_player_achievements(appid), self._url_user_stats(appid))
        for url in urls:
            try:
                with urllib.request.urlopen(url, timeout=10) as r:
                    data = json.loads(r.read())
            except Exception as e:
                errors.append(str(e))
                continue
            for a in data.get("playerstats", {}).get("achievements", []):
                if a.get("achieved"):
                    name = a.get("apiname") or a.get("name")
                    if name:
                        names.add(name)
        if len(errors) == len(urls):
            raise RuntimeError(f"both achievement endpoints failed: {errors}")
        return names

    def _url_player_achievements(self, appid: int) -> str:
        return f"{STEAM_API}/ISteamUserStats/GetPlayerAchievements/v1/?" + urllib.parse.urlencode({
            "key": self.api_key, "steamid": self.steamid, "appid": appid,
        })

    def _url_user_stats(self, appid: int) -> str:
        return f"{STEAM_API}/ISteamUserStats/GetUserStatsForGame/v2/?" + urllib.parse.urlencode({
            "key": self.api_key, "steamid": self.steamid, "appid": appid,
        })


@dataclass
class TickResult:
    fired: int = 0
    verified: int = 0
    skipped: int = 0
    failed: int = 0
    errors: int = 0


FireCallback = Callable[[int, str], tuple[bool, str]]


def make_subprocess_fire(unlocker_path: Path) -> FireCallback:
    def fire(appid: int, api_name: str) -> tuple[bool, str]:
        try:
            r = subprocess.run(
                [str(unlocker_path), "--appid", str(appid), "--achievement", api_name],
                capture_output=True, text=True, timeout=30,
            )
        except Exception as e:
            return False, f"subprocess: {e}"[:200]
        if r.returncode != 0:
            return False, (r.stderr or "").strip()[:200] or f"exit {r.returncode}"
        return True, ""
    return fire


class Orchestrator:
    def __init__(
        self,
        conn: sqlite3.Connection,
        fire: FireCallback,
        steam: SteamClient | None,
        now_func: Callable[[], datetime] | None = None,
    ):
        self.conn = conn
        self.fire = fire
        self.steam = steam
        self.now = now_func or (lambda: datetime.now(timezone.utc))

    def add_campaign(self, schedule_path: Path) -> int:
        schedule = json.loads(schedule_path.read_text())
        created = self.now().isoformat()
        cur = self.conn.execute(
            """INSERT INTO campaigns (appid, schedule_path, created_at, target_hours,
                   hours_source, current_playtime_at_plan, seed, jitter_sigma)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                int(schedule["appid"]),
                str(Path(schedule_path).resolve()),
                created,
                float(schedule["target_hours"]),
                schedule.get("hours_source"),
                float(schedule.get("current_playtime_hours", 0)),
                schedule.get("seed"),
                schedule.get("jitter_sigma"),
            ),
        )
        campaign_id = cur.lastrowid
        for u in schedule["unlocks"]:
            self.conn.execute(
                """INSERT INTO unlocks (campaign_id, api_name, display_name, global_percent,
                       in_game_hour, time_requirement_hours, unlock_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    campaign_id,
                    u["api_name"],
                    u.get("display_name"),
                    u.get("global_percent"),
                    float(u["in_game_hour"]),
                    u.get("time_requirement_hours"),
                    u["unlock_at"],
                ),
            )
        self.conn.commit()
        return campaign_id

    def list_campaigns(self) -> list[dict]:
        rows = self.conn.execute(
            """SELECT c.id, c.appid, c.state, c.target_hours, c.hours_source,
                      COUNT(u.id),
                      SUM(CASE WHEN u.state = 'verified' THEN 1 ELSE 0 END),
                      SUM(CASE WHEN u.state = 'fired'    THEN 1 ELSE 0 END),
                      SUM(CASE WHEN u.state = 'pending'  THEN 1 ELSE 0 END),
                      SUM(CASE WHEN u.state = 'failed'   THEN 1 ELSE 0 END)
               FROM campaigns c LEFT JOIN unlocks u ON u.campaign_id = c.id
               GROUP BY c.id ORDER BY c.id"""
        ).fetchall()
        keys = ["id", "appid", "state", "target_hours", "hours_source",
                "total", "verified", "fired", "pending", "failed"]
        return [dict(zip(keys, r)) for r in rows]

    def tick(self) -> TickResult:
        result = TickResult()
        campaigns = self.conn.execute(
            "SELECT id, appid FROM campaigns WHERE state = 'active'"
        ).fetchall()

        for campaign_id, appid in campaigns:
            if self.steam is None:
                result.errors += 1
                continue
            try:
                state = self.steam.get_state(appid)
            except Exception as e:
                print(
                    f"[{self._ts()}] campaign {campaign_id} (appid {appid}): "
                    f"fetch state failed: {e}",
                    file=sys.stderr,
                )
                result.errors += 1
                continue

            self._verify_unlocks(campaign_id, state.unlocked, result)
            unverifiable = self._is_unverifiable(appid)
            if not unverifiable:
                self._fire_due(campaign_id, appid, state, result)
            self._bump_verify_attempts(campaign_id)
            self._check_silent_failures(campaign_id, appid, result)
            self._maybe_complete_campaign(campaign_id)

        self.conn.commit()
        return result

    def _verify_unlocks(self, campaign_id: int, unlocked_set: set[str], result: TickResult):
        rows = self.conn.execute(
            "SELECT id, api_name FROM unlocks "
            "WHERE campaign_id = ? AND state IN ('pending', 'fired')",
            (campaign_id,),
        ).fetchall()
        now_iso = self._ts()
        for unlock_id, api_name in rows:
            if api_name in unlocked_set:
                self.conn.execute(
                    "UPDATE unlocks SET state = 'verified', verified_at = ? WHERE id = ?",
                    (now_iso, unlock_id),
                )
                result.verified += 1

    def _fire_due(
        self,
        campaign_id: int,
        appid: int,
        state: SteamState,
        result: TickResult,
    ):
        now_iso = self._ts()
        rows = self.conn.execute(
            """SELECT id, api_name, display_name, time_requirement_hours
               FROM unlocks
               WHERE campaign_id = ? AND state = 'pending' AND unlock_at <= ?
               ORDER BY unlock_at""",
            (campaign_id, now_iso),
        ).fetchall()

        for unlock_id, api_name, display_name, time_req in rows:
            if time_req is not None and state.playtime_hours < time_req:
                print(
                    f"[{now_iso}] hold {api_name}: playtime {state.playtime_hours:.1f}h "
                    f"< required {time_req}h",
                    file=sys.stderr,
                )
                result.skipped += 1
                continue

            print(
                f"[{now_iso}] firing {api_name} ({display_name or ''}) on appid {appid}",
                file=sys.stderr,
            )
            ok, err = self.fire(appid, api_name)
            if not ok:
                self.conn.execute(
                    "UPDATE unlocks SET last_error = ? WHERE id = ?",
                    (err, unlock_id),
                )
                print(f"  failed: {err}", file=sys.stderr)
                result.errors += 1
                continue

            self.conn.execute(
                "UPDATE unlocks SET state = 'fired', fired_at = ?, last_error = NULL "
                "WHERE id = ?",
                (now_iso, unlock_id),
            )
            result.fired += 1

    def _bump_verify_attempts(self, campaign_id: int):
        self.conn.execute(
            "UPDATE unlocks SET verify_attempts = verify_attempts + 1 "
            "WHERE campaign_id = ? AND state = 'fired'",
            (campaign_id,),
        )

    def _check_silent_failures(self, campaign_id: int, appid: int, result: TickResult):
        rows = self.conn.execute(
            "SELECT id FROM unlocks "
            "WHERE campaign_id = ? AND state = 'fired' AND verify_attempts >= ?",
            (campaign_id, MAX_VERIFY_ATTEMPTS),
        ).fetchall()
        if not rows:
            return

        self.conn.execute(
            "INSERT OR IGNORE INTO unverifiable_games (appid, first_detected_at, reason) "
            "VALUES (?, ?, ?)",
            (appid, self._ts(), f"{len(rows)} unlocks fired but never verified"),
        )
        self.conn.execute(
            "UPDATE unlocks SET state = 'failed', last_error = 'silent-failure' "
            "WHERE campaign_id = ? AND state IN ('fired', 'pending')",
            (campaign_id,),
        )
        self.conn.execute(
            "UPDATE campaigns SET state = 'invalid' WHERE id = ?",
            (campaign_id,),
        )
        result.failed += len(rows)
        print(
            f"[{self._ts()}] campaign {campaign_id} (appid {appid}): "
            f"marked unverifiable, {len(rows)} unlocks failed",
            file=sys.stderr,
        )

    def _is_unverifiable(self, appid: int) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM unverifiable_games WHERE appid = ?",
            (appid,),
        ).fetchone()
        return row is not None

    def _maybe_complete_campaign(self, campaign_id: int):
        row = self.conn.execute(
            "SELECT COUNT(*) FROM unlocks "
            "WHERE campaign_id = ? AND state IN ('pending', 'fired')",
            (campaign_id,),
        ).fetchone()
        if row and row[0] == 0:
            self.conn.execute(
                "UPDATE campaigns SET state = 'completed', completed_at = ? "
                "WHERE id = ? AND state = 'active'",
                (self._ts(), campaign_id),
            )

    def _ts(self) -> str:
        return self.now().isoformat()


def _print_list(conn: sqlite3.Connection):
    rows = Orchestrator(conn, lambda a, n: (False, "noop"), None).list_campaigns()
    if not rows:
        print("no campaigns")
        return
    print(f"  {'id':>3}  {'appid':>10}  {'state':<10}  {'verified':>10}  {'fired':>6}  {'pending':>8}  {'failed':>6}")
    for c in rows:
        print(
            f"  {c['id']:>3}  {c['appid']:>10}  {c['state']:<10}  "
            f"{(c['verified'] or 0):>3}/{c['total']:<6}  "
            f"{(c['fired'] or 0):>6}  {(c['pending'] or 0):>8}  {(c['failed'] or 0):>6}"
        )


def _print_status(conn: sqlite3.Connection, campaign_id: int) -> int:
    row = conn.execute(
        "SELECT appid, state, target_hours, hours_source, current_playtime_at_plan, created_at "
        "FROM campaigns WHERE id = ?",
        (campaign_id,),
    ).fetchone()
    if not row:
        print(f"no campaign {campaign_id}", file=sys.stderr)
        return 1
    appid, state, target, source, playtime_at_plan, created = row
    print(f"campaign {campaign_id}: appid={appid} state={state} target={target}h source={source or '-'}")
    print(f"  created={created} playtime_at_plan={playtime_at_plan}h")
    unlocks = conn.execute(
        "SELECT api_name, display_name, unlock_at, in_game_hour, state, fired_at, verified_at, "
        "verify_attempts, last_error "
        "FROM unlocks WHERE campaign_id = ? ORDER BY unlock_at",
        (campaign_id,),
    ).fetchall()
    for u in unlocks:
        api_name, display, unlock_at, in_game, st, fired_at, verified_at, attempts, err = u
        marker = {"pending": ".", "fired": "F", "verified": "V", "failed": "X"}.get(st, "?")
        print(
            f"  [{marker}] {unlock_at:32}  abs={in_game:6.1f}h  {api_name:30}  {display or ''}"
            + (f"  attempts={attempts}" if st == "fired" else "")
            + (f"  err={err}" if err else "")
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--unlocker", type=Path, default=DEFAULT_UNLOCKER)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_add = sub.add_parser("add")
    p_add.add_argument("schedule", type=Path)
    sub.add_parser("list")
    p_status = sub.add_parser("status")
    p_status.add_argument("campaign_id", type=int)
    sub.add_parser("tick")
    p_daemon = sub.add_parser("daemon")
    p_daemon.add_argument("--interval", type=int, default=60)
    for name in ("pause", "resume", "remove"):
        p = sub.add_parser(name)
        p.add_argument("campaign_id", type=int)

    args = ap.parse_args(argv)
    conn = init_db(args.db)

    if args.cmd == "list":
        _print_list(conn)
        return 0
    if args.cmd == "status":
        return _print_status(conn, args.campaign_id)
    if args.cmd == "pause":
        conn.execute("UPDATE campaigns SET state = 'paused' WHERE id = ?", (args.campaign_id,))
        conn.commit()
        print(f"paused {args.campaign_id}")
        return 0
    if args.cmd == "resume":
        conn.execute(
            "UPDATE campaigns SET state = 'active' WHERE id = ? AND state = 'paused'",
            (args.campaign_id,),
        )
        conn.commit()
        print(f"resumed {args.campaign_id}")
        return 0
    if args.cmd == "remove":
        conn.execute("DELETE FROM campaigns WHERE id = ?", (args.campaign_id,))
        conn.commit()
        print(f"removed {args.campaign_id}")
        return 0

    api_key = os.environ.get("STEAM_WEB_API_KEY") or os.environ.get("STEAM_API_KEY")
    steamid = os.environ.get("STEAM_ID")
    if not (api_key and steamid):
        print("STEAM_API_KEY (or STEAM_WEB_API_KEY) and STEAM_ID required", file=sys.stderr)
        return 1
    steam = SteamClient(api_key, steamid)
    orch = Orchestrator(conn, make_subprocess_fire(args.unlocker), steam)

    if args.cmd == "add":
        campaign_id = orch.add_campaign(args.schedule)
        print(f"added campaign {campaign_id}")
        return 0
    if args.cmd == "tick":
        res = orch.tick()
        print(
            f"tick: fired={res.fired} verified={res.verified} "
            f"skipped={res.skipped} failed={res.failed} errors={res.errors}",
            file=sys.stderr,
        )
        return 0
    if args.cmd == "daemon":
        print(f"daemon: interval={args.interval}s", file=sys.stderr)
        try:
            while True:
                try:
                    res = orch.tick()
                    print(
                        f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] "
                        f"fired={res.fired} verified={res.verified} "
                        f"skipped={res.skipped} failed={res.failed} errors={res.errors}",
                        file=sys.stderr,
                    )
                except Exception as e:
                    print(f"tick error: {e}", file=sys.stderr)
                time.sleep(args.interval)
        except KeyboardInterrupt:
            return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
