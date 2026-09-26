#!/usr/bin/env python3
"""
Build data.json for the poker-night screen from SettleStack and push it.

    update.py              # rebuild data.json once, commit + push if it changed
    update.py --watch      # poll every 45s; push whenever a new game is logged
    update.py --no-push    # rebuild only

Tonight = every game with id > BASELINE_GAME_ID (the last game before tonight),
so games logged from Discord show up without any ids being typed in.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

HERE = Path(__file__).resolve().parent
SS = Path("/Users/john/dev/settle-stack")
sys.path.insert(0, str(SS / "agent"))
import settlestack_db as ss  # noqa: E402

CONFIG = json.loads((HERE / "config.json").read_text())


def display(username: str) -> str:
    return CONFIG["names"].get(username.strip(), username.strip())


def num(v) -> float:
    return round(float(v), 2) if isinstance(v, (Decimal, int, float)) else 0.0


def build() -> dict:
    with ss._conn() as c:
        ids = [r["id"] for r in c.execute(
            "SELECT id FROM games WHERE id > %s ORDER BY start_time, id",
            (CONFIG["baseline_game_id"],)).fetchall()]

    games, totals, played = [], {}, {}
    for n, gid in enumerate(ids, 1):
        g = ss.get_game(gid)
        if not g:
            continue
        rows = []
        for p in g["players"]:
            name = display(p["username"])
            net = num(p["net"])
            rows.append({"name": name, "buy_in": num(p["buy_in"]),
                         "cashout": num(p["final_balance"]), "net": net})
            totals[name] = round(totals.get(name, 0) + net, 2)
            played[name] = played.get(name, 0) + 1
        games.append({"n": n, "id": gid, "players": rows})

    standings = [{"name": k, "net": v, "games": played[k]}
                 for k, v in sorted(totals.items(), key=lambda kv: -kv[1])]
    settlements = [{"from": a, "to": b, "amount": amt}
                   for a, b, amt in ss.settle(totals)]

    featured = []
    for username in CONFIG["featured"]:
        p = ss.player_profile(username)
        if not p:
            continue
        g = p["games"] or 0
        featured.append({
            "name": display(p["username"]),
            "games": g,
            "wins": p["wins"],
            "losses": p["losses"],
            "win_rate": round(100 * p["wins"] / g) if g else 0,
            "lifetime_net": num(p["lifetime_net"]),
            "best_night": num(p["best_night"]),
            "worst_night": num(p["worst_night"]),
            "busted": p["times_busted"],
            "tonight": totals.get(display(p["username"])),
        })
    featured.sort(key=lambda f: -f["lifetime_net"])

    return {
        "title": CONFIG["title"],
        "host": CONFIG["host"],
        "date": CONFIG["date"],
        "games": games,
        "standings": standings,
        "settlements": settlements,
        "featured": featured,
    }


def write(data: dict) -> bool:
    path = HERE / "data.json"
    old = json.loads(path.read_text()) if path.exists() else {}
    old.pop("updated_at", None)
    if old == data:
        return False
    data = {**data, "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    path.write_text(json.dumps(data, indent=2) + "\n")
    return True


def push(msg: str) -> None:
    git = ["git", "-C", str(HERE)]
    subprocess.run(git + ["add", "data.json"], check=True)
    subprocess.run(git + ["commit", "-q", "-m", msg], check=True)
    subprocess.run(git + ["push", "-q"], check=True)


def once(do_push: bool) -> None:
    data = build()
    if write(data):
        print(f"{datetime.now():%H:%M:%S} updated: {len(data['games'])} game(s)")
        if do_push:
            push(f"Update: {len(data['games'])} game(s)")
    else:
        print(f"{datetime.now():%H:%M:%S} no change")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--watch", action="store_true")
    ap.add_argument("--no-push", action="store_true")
    ap.add_argument("--interval", type=int, default=45)
    a = ap.parse_args()
    if not a.watch:
        once(not a.no_push)
        return 0
    while True:
        try:
            once(not a.no_push)
        except Exception as e:  # keep watching through a DB blip
            print(f"{datetime.now():%H:%M:%S} error: {e}", flush=True)
        sys.stdout.flush()
        time.sleep(a.interval)


if __name__ == "__main__":
    raise SystemExit(main())
