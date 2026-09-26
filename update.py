#!/usr/bin/env python3
"""
Build data.json for the poker-night screen from SettleStack and push it.

    update.py              # rebuild data.json once, commit + push if it changed
    update.py --watch      # poll every 45s; push whenever a new game is logged
    update.py --no-push    # rebuild only
    update.py --watch --exit-on-new   # stop when a game needs a hand-written roast

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
import stats  # noqa: E402

CONFIG = json.loads((HERE / "config.json").read_text())


def display(username: str) -> str:
    return CONFIG["names"].get(username.strip(), username.strip())


def num(v) -> float:
    return round(float(v), 2) if isinstance(v, (Decimal, int, float)) else 0.0


def session_stats(name: str, games: list[dict]) -> dict:
    """Tonight-only numbers for one player across every game they sat in."""
    seats = [p for g in games for p in g["players"] if p["name"] == name]
    nets = [p["net"] for p in seats]
    default = CONFIG.get("default_buy_in", 20)
    return {
        "won": sum(1 for n in nets if n > 0),
        "best": max(nets) if nets else 0,
        "worst": min(nets) if nets else 0,
        "bought_in": round(sum(p["buy_in"] for p in seats), 2),
        "rebuys": sum(max(0, round((p["buy_in"] - default) / default)) for p in seats),
        "busts": sum(1 for p in seats if p["cashout"] <= 0.01),
    }


def auto_recap(game: dict) -> dict:
    """Placeholder trash talk shown until Claude writes the real one."""
    ps = game["players"]
    top, bottom = ps[0], ps[-1]
    lines = {top["name"]: f"Took the pot for {money(top['net'])}. Insufferable for at least a week."}
    if bottom["net"] < 0 and bottom is not top:
        lines[bottom["name"]] = f"Donated {money(-bottom['net'])} to the cause. Generous. Tragic."
    return {
        "headline": f"{top['name']} takes Game {game['n']}",
        "recap": f"{top['name']} walks away up {money(top['net'])}; "
                 f"{bottom['name']} is funding tonight's snacks.",
        "lines": lines,
        "auto": True,
    }


def money(v: float) -> str:
    return f"${abs(v):.2f}"


def history(usernames: list[str]) -> dict:
    """Cumulative lifetime net per poker night (Chicago date) for each player."""
    with ss._conn() as c:
        rows = c.execute(
            """SELECT u.username,
                      (g.start_time AT TIME ZONE 'America/Chicago')::date AS night,
                      sum(p.final_balance - p.buy_in) AS net
               FROM players p JOIN users u ON u.id = p.user_id
               JOIN games g ON g.id = p.game_id
               WHERE u.username = ANY(%s) AND g.start_time IS NOT NULL
               GROUP BY 1, 2 ORDER BY 2""", (usernames,)).fetchall()
    out: dict[str, list] = {}
    run: dict[str, float] = {}
    for r in rows:
        name = display(r["username"])
        run[name] = round(run.get(name, 0) + num(r["net"]), 2)
        out.setdefault(name, []).append([r["night"].isoformat(), run[name]])
    return out


def build() -> dict:
    with ss._conn() as c:
        ids = [r["id"] for r in c.execute(
            "SELECT id FROM games WHERE id > %s AND NOT (id = ANY(%s)) ORDER BY start_time, id",
            (CONFIG["baseline_game_id"], CONFIG.get("exclude_game_ids", []))).fetchall()]

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

    roasts = json.loads((HERE / "roasts.json").read_text() or "{}")
    for g in games:
        g["recap"] = roasts.get(str(g["id"])) or auto_recap(g)

    standings = [{"name": k, "net": v, "games": played[k], **session_stats(k, games)}
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

    newcomers = [{"name": display(u), "tonight": totals.get(display(u))}
                 for u in CONFIG.get("newcomers", [])]

    return {
        "newcomers": newcomers,
        "final": CONFIG.get("final", False),
        "finale": CONFIG.get("finale"),
        "history": history(CONFIG["featured"]),
        "colors": CONFIG.get("colors", {}),
        "stats": stats.compute(ss, CONFIG["featured"], display),
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
    subprocess.run(git + ["add", "data.json", "roasts.json"], check=True)
    subprocess.run(git + ["commit", "-q", "-m", msg], check=True)
    subprocess.run(git + ["push", "-q"], check=True)


def once(do_push: bool) -> list[int]:
    """Rebuild and publish. Returns ids of games still on the auto recap."""
    data = build()
    if write(data):
        print(f"{datetime.now():%H:%M:%S} updated: {len(data['games'])} game(s)")
        if do_push:
            push(f"Update: {len(data['games'])} game(s)")
    else:
        print(f"{datetime.now():%H:%M:%S} no change")
    return [g["id"] for g in data["games"] if g["recap"].get("auto")]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--watch", action="store_true")
    ap.add_argument("--no-push", action="store_true")
    ap.add_argument("--interval", type=int, default=45)
    ap.add_argument("--exit-on-new", action="store_true",
                    help="stop once a game has no hand-written roast yet")
    a = ap.parse_args()
    if not a.watch:
        once(not a.no_push)
        return 0
    while True:
        try:
            pending = once(not a.no_push)
            # Exit so the Claude session is notified and can write the roast.
            if a.exit_on_new and pending:
                print(f"NEEDS_ROAST {pending}", flush=True)
                return 0
        except Exception as e:  # keep watching through a DB blip
            print(f"{datetime.now():%H:%M:%S} error: {e}", flush=True)
        sys.stdout.flush()
        time.sleep(a.interval)


if __name__ == "__main__":
    raise SystemExit(main())
