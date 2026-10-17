"""Nightly ESPN sync.

Reads ESPN_S2 and ESPN_SWID from the environment. Pulls teams, rosters, and
draft results for the season in data/config.json, then updates:

  data/espn/rosters.json   current rosters and records (read by the site)
  data/espn/report.json    matching problems and warnings
  data/contracts.csv       fills espn_id, moves traded players, releases
                           dropped players, adds newly drafted rookies
  data/franchises.csv      fills espn_team_id
  data/events.csv          log of every automatic change

Release detection compares against the previous ESPN snapshot, so nothing is
released on the first run or right after the season number changes.

Usage:  python scripts/fetch_espn.py [--dry-run]
"""

import csv
import json
import os
import re
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
ESPN_DIR = DATA / "espn"

API = ("https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/"
       "{season}/segments/0/leagues/{league}")

POSITIONS = {1: "QB", 2: "RB", 3: "WR", 4: "TE", 5: "K", 16: "D/ST"}
SLOTS = {0: "QB", 2: "RB", 4: "WR", 6: "TE", 7: "SFLEX", 16: "D/ST", 17: "K",
         20: "Bench", 21: "IR", 23: "FLEX"}
PRO_TEAMS = {0: "FA", 1: "ATL", 2: "BUF", 3: "CHI", 4: "CIN", 5: "CLE", 6: "DAL",
             7: "DEN", 8: "DET", 9: "GB", 10: "TEN", 11: "IND", 12: "KC", 13: "LV",
             14: "LAR", 15: "MIA", 16: "MIN", 17: "NE", 18: "NO", 19: "NYG",
             20: "NYJ", 21: "PHI", 22: "ARI", 23: "PIT", 24: "LAC", 25: "SF",
             26: "SEA", 27: "TB", 28: "WSH", 29: "CAR", 30: "JAX", 33: "BAL",
             34: "HOU"}

# Workbook D/ST names -> ESPN nickname ("Broncos D/ST").
DST_ALIASES = {
    "arizona": "cardinals", "atlanta": "falcons", "baltimore": "ravens",
    "buffalo": "bills", "carolina": "panthers", "chicago": "bears",
    "cincinnati": "bengals", "cleveland": "browns", "dallas": "cowboys",
    "denver": "broncos", "detroit": "lions", "green bay": "packers",
    "houston": "texans", "indianapolis": "colts", "jacksonville": "jaguars",
    "kansas": "chiefs", "kansas city": "chiefs", "las vegas": "raiders",
    "la chargers": "chargers", "la rams": "rams", "los angeles rams": "rams",
    "los angeles chargers": "chargers", "miami": "dolphins",
    "minnesota": "vikings", "new england": "patriots", "new orleans": "saints",
    "ny giants": "giants", "ny jets": "jets", "philadelphia": "eagles",
    "pittsburgh": "steelers", "san francisco": "49ers", "seattle": "seahawks",
    "tampa bay": "buccaneers", "tennessee": "titans", "washington": "commanders",
}

CONTRACT_FIELDS = ["player", "pos", "franchise", "draft_year", "price", "years",
                   "status", "dead_from", "espn_id", "note"]
EVENT_FIELDS = ["date", "season", "type", "player", "franchise", "to_franchise",
                "detail"]


def norm(name):
    """Normalize a player name for matching."""
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    s = s.lower().replace("d/st", "").strip()
    s = re.sub(r"[^a-z0-9 ]", "", s)
    s = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return DST_ALIASES.get(s, s)


def read_csv(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def write_csv(path, rows, fields):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def get(season, league, views, cookies):
    url = API.format(season=season, league=league)
    r = requests.get(url, params=[("view", v) for v in views], cookies=cookies,
                     headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
    if r.status_code in (401, 403):
        sys.exit(f"ESPN returned {r.status_code}. The espn_s2/SWID cookies have "
                 "probably expired. Update the ESPN_S2 and ESPN_SWID repo secrets.")
    r.raise_for_status()
    return r.json()


def team_name(t):
    return t.get("name") or f"{t.get('location', '')} {t.get('nickname', '')}".strip()


def main(dry_run=False):
    cfg = json.loads((DATA / "config.json").read_text())
    season, league = cfg["current_season"], cfg["espn_league_id"]
    cookies = {"espn_s2": os.environ["ESPN_S2"], "SWID": os.environ["ESPN_SWID"]}
    now = datetime.now(timezone.utc)
    today = now.date().isoformat()

    data = get(season, league, ["mTeam", "mRoster", "mSettings", "mDraftDetail"], cookies)
    ESPN_DIR.mkdir(parents=True, exist_ok=True)
    (ESPN_DIR / "raw_latest.json").write_text(json.dumps(data, indent=1))

    franchises = read_csv(DATA / "franchises.csv")
    contracts = read_csv(DATA / "contracts.csv")
    events_path = DATA / "events.csv"
    events = read_csv(events_path) if events_path.exists() else []
    report = {"updated": now.isoformat(timespec="seconds"), "season": season,
              "warnings": [], "unmatched_contracts": [], "unmatched_teams": []}

    def log(kind, player, fid, to="", detail=""):
        events.append({"date": today, "season": season, "type": kind, "player": player,
                       "franchise": fid, "to_franchise": to, "detail": detail})

    # ---- Teams -> franchises (match on owner last names) ----
    members = {m["id"]: m for m in data.get("members", [])}
    by_espn = {f["espn_team_id"]: f["id"] for f in franchises if f["espn_team_id"]}
    for t in data["teams"]:
        tid = str(t["id"])
        if tid in by_espn:
            continue
        people = [members.get(o, {}) for o in t.get("owners", [])]
        full = {norm(f"{m.get('firstName', '')} {m.get('lastName', '')}") for m in people} - {""}
        last = {norm(m.get("lastName", "")) for m in people} - {""}
        open_f = [f for f in franchises if not f["espn_team_id"]]
        hits = [f for f in open_f if any(n in norm(f["owners"]) for n in full)]
        if not hits:
            hits = [f for f in open_f if any(n in norm(f["owners"]) for n in last)]
        if len(hits) == 1:
            hits[0]["espn_team_id"] = tid
            by_espn[tid] = hits[0]["id"]
        else:
            owners = [f"{members.get(o, {}).get('firstName', '')} "
                      f"{members.get(o, {}).get('lastName', '')}" for o in t.get("owners", [])]
            report["unmatched_teams"].append({"espn_team_id": tid, "name": team_name(t),
                                              "owners": owners})

    # ---- Rosters ----
    rosters, records, where = {}, {}, {}
    for t in data["teams"]:
        fid = by_espn.get(str(t["id"]))
        if not fid:
            continue
        rec = t.get("record", {}).get("overall", {})
        records[fid] = {"team_name": team_name(t), "wins": rec.get("wins", 0),
                        "losses": rec.get("losses", 0), "ties": rec.get("ties", 0),
                        "points_for": rec.get("pointsFor", 0)}
        rows = []
        for e in t.get("roster", {}).get("entries", []):
            p = e.get("playerPoolEntry", {}).get("player", {})
            pid = str(e.get("playerId", p.get("id")))
            row = {
                "espn_id": pid,
                "name": p.get("fullName", ""),
                "pos": POSITIONS.get(p.get("defaultPositionId"), ""),
                "nfl": PRO_TEAMS.get(p.get("proTeamId"), ""),
                "slot": SLOTS.get(e.get("lineupSlotId"), str(e.get("lineupSlotId"))),
                "injury": p.get("injuryStatus", ""),
                "acq_type": e.get("acquisitionType", ""),
                "acq_date": e.get("acquisitionDate", 0),
            }
            rows.append(row)
            where[pid] = (fid, row)
        rosters[fid] = rows
        if len(rows) > cfg["roster_limit"]:
            report["warnings"].append(f"{fid} has {len(rows)} players on the roster.")

    # ---- Previous snapshot (for drop detection) ----
    prev_path = ESPN_DIR / "rosters.json"
    prev = json.loads(prev_path.read_text()) if prev_path.exists() else {}
    prev_ok = prev.get("source") == "espn" and prev.get("season") == season
    prev_where = {}
    if prev_ok:
        for fid, rows in prev["teams"].items():
            for r in rows:
                prev_where[r["espn_id"]] = (fid, r)

    draft = data.get("draftDetail", {})
    drafted = bool(draft.get("drafted"))

    # ---- Match contracts to ESPN players ----
    active = [c for c in contracts if c["status"] == "active"]
    for c in active:
        if c["espn_id"]:
            continue
        key = norm(c["player"])
        cands = [r for r in rosters.get(c["franchise"], []) if norm(r["name"]) == key]
        if not cands:
            cands = [r for rows in rosters.values() for r in rows if norm(r["name"]) == key]
        if len(cands) == 1:
            c["espn_id"] = cands[0]["espn_id"]
        else:
            report["unmatched_contracts"].append(
                {"player": c["player"], "franchise": c["franchise"],
                 "reason": "no match" if not cands else "multiple matches"})

    # ---- Expire, trade, release ----
    for c in active:
        dy, yrs = int(c["draft_year"]), int(c["years"] or 0)
        last_year = dy + 1 + yrs
        pid = c["espn_id"]
        if last_year < season:
            c["status"] = "expired"
            log("expired", c["player"], c["franchise"], detail=f"last season {last_year}")
            continue
        if not pid or not prev_ok:
            continue
        now_at = where.get(pid)
        was_at = prev_where.get(pid)
        if was_at is None:
            continue  # was not on a roster last time either; leave for review
        moved = now_at is None or now_at[1]["acq_date"] != was_at[1]["acq_date"]
        if not moved:
            continue
        if now_at and now_at[1]["acq_type"] == "TRADE":
            old = c["franchise"]
            c["franchise"] = now_at[0]
            log("trade", c["player"], old, now_at[0], "contract moves with player")
        else:
            c["status"] = "released"
            dead_from = season + 1 if drafted else season
            if yrs > 0 and dead_from > last_year:
                log("release", c["player"], c["franchise"],
                    detail=f"contract ends after {last_year}, no dead money")
            elif yrs > 0:
                c["dead_from"] = str(dead_from)
                cost = int(c["price"]) + cfg["extension_step"] * (yrs - 1)
                log("release", c["player"], c["franchise"],
                    detail=f"dead money ${cost // 2}/yr, drafts {c['dead_from']}-{last_year}")
            else:
                log("release", c["player"], c["franchise"], detail="rookie deal, no dead money")

    # ---- New rookies from the auction ----
    known = {c["espn_id"] for c in contracts if c["status"] == "active" and c["espn_id"]}
    for pick in draft.get("picks", []):
        pid = str(pick.get("playerId"))
        fid = by_espn.get(str(pick.get("teamId")))
        if pick.get("keeper") or pid in known or not fid:
            continue
        name, pos = pid, ""
        if pid in where:
            name, pos = where[pid][1]["name"], where[pid][1]["pos"]
        contracts.append({"player": name, "pos": pos, "franchise": fid,
                          "draft_year": season, "price": pick.get("bidAmount", 0),
                          "years": "", "status": "active", "dead_from": "",
                          "espn_id": pid, "note": ""})
        known.add(pid)
        log("drafted", name, fid, detail=f"${pick.get('bidAmount', 0)}")

    # ---- Keeper check after the draft ----
    if drafted:
        for pick in draft.get("picks", []):
            if pick.get("keeper") and str(pick.get("playerId")) not in known:
                report["warnings"].append(
                    f"ESPN keeper {pick.get('playerId')} has no active contract.")

    # ---- Contracted players not on any roster ----
    for c in contracts:
        if c["status"] == "active" and c["espn_id"] and c["espn_id"] not in where:
            report["warnings"].append(
                f"{c['player']} ({c['franchise']}) has an active contract but is not on an ESPN roster.")

    if dry_run:
        print(json.dumps(report, indent=1))
        return

    snapshot = {"source": "espn", "season": season,
                "updated": now.isoformat(timespec="seconds"),
                "draft_complete": drafted, "teams": rosters, "records": records}
    prev_path.write_text(json.dumps(snapshot, indent=1))
    (ESPN_DIR / "report.json").write_text(json.dumps(report, indent=1))
    write_csv(DATA / "contracts.csv", contracts, CONTRACT_FIELDS)
    write_csv(DATA / "franchises.csv", franchises, ["id", "team_name", "owners", "council", "espn_team_id"])
    write_csv(events_path, events, EVENT_FIELDS)
    print(f"Synced {sum(len(r) for r in rosters.values())} roster spots, "
          f"{len(report['unmatched_contracts'])} unmatched contracts, "
          f"{len(report['warnings'])} warnings.")


if __name__ == "__main__":
    main(dry_run="--dry-run" in sys.argv)
