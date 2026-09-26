"""
Fun all-time stats for the rotating slides on the pre-game screen.

Everything is computed from SettleStack's per-game player rows and the
per-game settlement transfers (games.transactions: payer_id -> payee_id).
"""
from __future__ import annotations

from collections import defaultdict
from itertools import combinations


def compute(ss, usernames: list[str], display) -> dict:
    with ss._conn() as c:
        crew = c.execute(
            "SELECT id, username FROM users WHERE username = ANY(%s)", (usernames,)).fetchall()
        crew_ids = {r["id"]: display(r["username"]) for r in crew}
        names = {r["id"]: display(r["username"])
                 for r in c.execute("SELECT id, username FROM users").fetchall()}
        rows = c.execute(
            """SELECT p.game_id, p.user_id, p.buy_in, p.final_balance,
                      (p.final_balance - p.buy_in) AS net,
                      (g.start_time AT TIME ZONE 'America/Chicago') AS t
               FROM players p JOIN games g ON g.id = p.game_id
               WHERE g.start_time IS NOT NULL
               ORDER BY g.start_time, g.id""").fetchall()
        txns = c.execute(
            "SELECT id, transactions FROM games WHERE jsonb_array_length(transactions) > 0"
        ).fetchall()

    games: dict[int, dict[int, dict]] = defaultdict(dict)
    order: list[int] = []
    for r in rows:
        if r["game_id"] not in games:
            order.append(r["game_id"])
        games[r["game_id"]][r["user_id"]] = {
            "net": float(r["net"]), "buy_in": float(r["buy_in"]),
            "cashout": float(r["final_balance"]), "t": r["t"]}

    crew_names = [crew_ids[i] for i in crew_ids]

    # --- head to head: in games both played, who finished with more ---------
    h2h = {a: {b: {"ahead": 0, "games": 0} for b in crew_names} for a in crew_names}
    for gid in order:
        seated = [u for u in games[gid] if u in crew_ids]
        for a, b in combinations(seated, 2):
            na, nb = games[gid][a]["net"], games[gid][b]["net"]
            A, B = crew_ids[a], crew_ids[b]
            h2h[A][B]["games"] += 1
            h2h[B][A]["games"] += 1
            if na > nb + 0.005:
                h2h[A][B]["ahead"] += 1
            elif nb > na + 0.005:
                h2h[B][A]["ahead"] += 1

    # --- money flow: who has paid whom, all time -----------------------------
    paid = defaultdict(float)  # (payer, payee) -> $
    for g in txns:
        for t in g["transactions"] or []:
            try:
                paid[(t["payer_id"], t["payee_id"])] += float(t["amount"])
            except (KeyError, TypeError, ValueError):
                continue

    nemesis = []
    for uid, name in crew_ids.items():
        out = {pe: amt for (pr, pe), amt in paid.items() if pr == uid}
        inc = {pr: amt for (pr, pe), amt in paid.items() if pe == uid}
        nem = max(out.items(), key=lambda kv: kv[1], default=None)
        atm = max(inc.items(), key=lambda kv: kv[1], default=None)
        nemesis.append({
            "name": name,
            "nemesis": names.get(nem[0]) if nem else None,
            "paid": round(nem[1], 2) if nem else 0,
            "atm": names.get(atm[0]) if atm else None,
            "received": round(atm[1], 2) if atm else 0,
        })

    # crew-to-crew net flow (positive = row has taken money from column)
    flow = {a: {b: 0.0 for b in crew_names} for a in crew_names}
    for (pr, pe), amt in paid.items():
        if pr in crew_ids and pe in crew_ids:
            flow[crew_ids[pe]][crew_ids[pr]] += amt
            flow[crew_ids[pr]][crew_ids[pe]] -= amt
    flow = {a: {b: round(v, 2) for b, v in row.items()} for a, row in flow.items()}

    # --- per-player game series: records, streaks, swings, months ------------
    per = {n: [] for n in crew_names}
    for gid in order:
        for uid, p in games[gid].items():
            if uid in crew_ids:
                per[crew_ids[uid]].append({**p, "game": gid})

    records, swings, months = [], {}, defaultdict(lambda: defaultdict(float))
    biggest = []
    for name, seats in per.items():
        nets = [s["net"] for s in seats]
        best_streak = cur = 0
        for n in nets:
            cur = cur + 1 if n > 0 else 0
            best_streak = max(best_streak, cur)
        # current streak: + for wins, - for losses
        streak = 0
        for n in reversed(nets):
            if n > 0 and streak >= 0:
                streak += 1
            elif n < 0 and streak <= 0:
                streak -= 1
            else:
                break
        records.append({
            "name": name,
            "games": len(nets),
            "win_streak": best_streak,
            "streak": streak,
            "busts": sum(1 for s in seats if s["cashout"] <= 0.01),
            "rebuys": sum(1 for s in seats if s["buy_in"] > 20.01),
            "avg": round(sum(nets) / len(nets), 2) if nets else 0,
            "cashed": round(100 * sum(1 for n in nets if n > 0) / len(nets)) if nets else 0,
        })
        swings[name] = [round(n, 2) for n in nets]
        for s in seats:
            months[name][s["t"].strftime("%Y-%m")] += s["net"]
            biggest.append({"name": name, "net": round(s["net"], 2),
                            "date": s["t"].date().isoformat()})

    biggest.sort(key=lambda b: -b["net"])
    all_months = sorted({m for v in months.values() for m in v})

    return {
        "h2h": h2h,
        "flow": flow,
        "nemesis": nemesis,
        "records": records,
        "swings": swings,
        "months": {"cols": all_months,
                   "rows": {n: [round(months[n].get(m, 0), 2) if m in months[n] else None
                                for m in all_months] for n in crew_names}},
        "fame": biggest[:5],
        "shame": biggest[::-1][:5],
    }
