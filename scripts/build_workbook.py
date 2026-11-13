"""Build the league workbook (one sheet per team plus a LeagueInfo summary).

Reads the same data the website uses:
  data/config.json, data/franchises.csv, data/contracts.csv,
  data/cap_trades.csv, data/espn/rosters.json

Writes downloads/StarWars_DynastyFF.xlsx. The website's download button saves
it as StarWars_DynastyFF_YYYYMMDD.xlsx. downloads/workbook.json records a hash
of the inputs, so the file is only rebuilt (and committed) when data changes.
All cap numbers are live Excel formulas that follow the rules in assets/app.js
(teamYear, costIn, deadPerYear). If those rules change, change CAP RULES below.

Usage:  python scripts/build_workbook.py [--force]
"""

import argparse
import csv
import hashlib
import json
import re
import unicodedata
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from openpyxl import Workbook
from openpyxl.comments import Comment
from openpyxl.formatting.rule import FormulaRule
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.workbook.defined_name import DefinedName
from openpyxl.worksheet.datavalidation import DataValidation

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

# ---------- Style (website palette) ----------
FONT = "Calibri"
NAVY, HULL, GOLD, EMBER = "151D42", "1B2552", "E2B24A", "C0392B"
FILL = {  # light tints of the site's status colors
    "c": "DAE6F7",  # contract
    "b": "F7EFC4",  # Year B
    "a": "DCEFE0",  # Year A
    "d": "FADBD3",  # extend or release
    "fa": "E4E6EE",  # free agent
}
INPUT_BLUE = "1F4FD8"
MONEY = '"$"#,##0;[Red]-"$"#,##0;"–"'
MONEY_SIGNED = '+"$"#,##0;[Red]-"$"#,##0;"$"0'
YEAR = "0;-0;;@"  # hide zeros
thin = Side(style="thin", color="C9CEDF")
BOX = Border(left=thin, right=thin, top=thin, bottom=thin)


def fill(hex_):
    return PatternFill("solid", start_color=hex_, end_color=hex_)


def font(**kw):
    return Font(name=FONT, **{"size": 11, **kw})


def style(c, *, f=None, bg=None, fmt=None, align=None, border=True):
    c.font = f or font()
    if bg:
        c.fill = fill(bg)
    if fmt:
        c.number_format = fmt
    if align:
        c.alignment = Alignment(horizontal=align, vertical="center")
    if border:
        c.border = BOX


def header(ws, row, cols_labels, *, bg=NAVY):
    for col, label in cols_labels:
        c = ws[f"{col}{row}"]
        c.value = label
        style(c, f=font(bold=True, color=GOLD if bg == NAVY else "FFFFFF"), bg=bg, align="center")


# ---------- Data (mirrors assets/app.js) ----------
def read_csv(name):
    with open(DATA / name, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def norm(s):
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch)).lower()
    s = s.replace("d/st", "")
    s = re.sub(r"[^a-z0-9 ]", "", s)
    s = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b", "", s)
    return re.sub(r"\s+", " ", s).strip()


def last_year(c):
    return c["draft_year"] + 1 + c["years"]


def annual_cost(c, step):
    return c["price"] + (step * (c["years"] - 1) if c["years"] else 0)


def load():
    cfg = json.loads((DATA / "config.json").read_text())
    franchises = read_csv("franchises.csv")
    contracts = []
    for c in read_csv("contracts.csv"):
        contracts.append({**c, "draft_year": int(c["draft_year"]), "price": int(c["price"] or 0),
                          "years": int(c["years"] or 0),
                          "dead_from": int(c["dead_from"]) if c["dead_from"] else None})
    trades = [{**t, "draft_year": int(t["draft_year"]), "amount": int(t["amount"])}
              for t in read_csv("cap_trades.csv")]
    rp = DATA / "espn" / "rosters.json"
    rosters = json.loads(rp.read_text()) if rp.exists() else None
    return cfg, franchises, contracts, trades, rosters


def upcoming(cfg, rosters):
    cur = cfg["current_season"]
    if not rosters or rosters.get("source") != "espn":
        return cur + 1
    if rosters["season"] < cur or rosters.get("draft_complete") is False:
        return cur
    return cur + 1


def current_roster(fid, cur, contracts, rosters):
    """Same join as currentRoster() in app.js. Returns (player, contract) pairs."""
    active = [c for c in contracts if c["franchise"] == fid and c["status"] == "active"
              and (cur <= last_year(c) or (not c["years"] and cur == c["draft_year"] + 2))]
    used = set()
    rows = []
    for p in (rosters or {}).get("teams", {}).get(fid, []):
        def same(k):
            if k.get("espn_id") and p.get("espn_id"):
                return str(k["espn_id"]) == str(p["espn_id"])
            return norm(k["player"]) == norm(p["name"])
        c = next((k for i, k in enumerate(active) if i not in used and same(k)), None)
        if c is not None:
            used.add(active.index(c))
        rows.append((p, c))
    missing = [c for i, c in enumerate(active) if i not in used]
    rows += [({"name": c["player"], "pos": c["pos"], "nfl": ""}, c) for c in missing]

    def rank(pc):
        p, c = pc
        if not c:
            return (0, 0)
        n = cur - c["draft_year"] + 1
        r = 1 if n <= 1 else 2 if n == 2 else 3 if c["years"] else 2.5
        cost = c["price"] if n <= 2 else annual_cost(c, 5)
        return (r, cost)
    rows.sort(key=lambda pc: (-rank(pc)[0], -rank(pc)[1], (pc[1] or {}).get("player") or pc[0]["name"]))
    return rows


def sheet_name(fid):
    return "_".join("McMillan" if p == "mcmillan" else p.capitalize() for p in fid.split("_"))


# ---------- Workbook ----------
OUT = ROOT / "downloads"
XLSX = OUT / "StarWars_DynastyFF.xlsx"
META = OUT / "workbook.json"


def input_hash():
    """Hash of everything the workbook shows, minus the sync timestamp."""
    h = hashlib.sha256(Path(__file__).read_bytes())
    for name in ("config.json", "franchises.csv", "contracts.csv", "cap_trades.csv"):
        h.update((DATA / name).read_bytes())
    rp = DATA / "espn" / "rosters.json"
    if rp.exists():
        r = json.loads(rp.read_text())
        r.pop("updated", None)
        h.update(json.dumps(r, sort_keys=True).encode())
    return h.hexdigest()


def build(stamp):
    cfg, franchises, contracts, trades, rosters = load()
    cur = cfg["current_season"]
    nxt = upcoming(cfg, rosters)
    step = cfg["extension_step"]
    records = (rosters or {}).get("records", {}) if (rosters or {}).get("season") == cur else {}

    wb = Workbook()
    info = wb.active
    info.title = "LeagueInfo"
    info.sheet_properties.tabColor = GOLD

    # Named settings. Every team sheet reads these.
    names = {"SEASON": "C5", "DRAFT_YEAR": "C6", "CAP": "C7", "STEP": "C8"}
    for n, cell in names.items():
        col, row = cell[0], cell[1:]
        wb.defined_names[n] = DefinedName(n, attr_text=f"LeagueInfo!${col}${row}")

    team_refs = []  # filled per team, used by the summary table
    for f in franchises:
        fid = f["id"]
        ws = wb.create_sheet(sheet_name(fid))
        team_refs.append((f, ws.title, build_team(ws, f, cur, nxt, step, contracts, trades, rosters, cfg)))

    build_info(info, cfg, cur, nxt, team_refs, records, stamp)
    wb.calculation.fullCalcOnLoad = True  # openpyxl stores no results; Excel computes on open

    OUT.mkdir(parents=True, exist_ok=True)
    wb.save(XLSX)
    return XLSX


def build_team(ws, f, cur, nxt, step, contracts, trades, rosters, cfg):
    fid = f["id"]
    ws.sheet_view.showGridLines = False
    widths = {"A": 2, "B": 4, "C": 22, "D": 7, "E": 7, "F": 9, "G": 7, "H": 7, "I": 9,
              "J": 9, "K": 10, "L": 9, "M": 11, "N": 3, "O": 4, "P": 22, "Q": 9, "R": 9,
              "S": 9, "T": 10, "U": 9}
    for k, v in widths.items():
        ws.column_dimensions[k].width = v

    # Title
    ws.merge_cells("B2:M2")
    ws["B2"] = f"{f['team_name']}  ·  {f['owners']}"
    ws["B2"].font = font(size=16, bold=True, color=NAVY)

    # ---------- Roster (B:M) ----------
    header(ws, 3, [("B", "#"), ("C", "Player"), ("D", "Pos"), ("E", "NFL"), ("F", "Drafted"),
                   ("G", "Year"), ("H", "Keep?"), ("I", "Contract"), ("J", "Base Price"),
                   ("K", None), ("L", "Expires"), ("M", None)])
    ws["K3"] = '=SEASON&" cost"'
    ws["M3"] = '=DRAFT_YEAR&" cost"'

    roster = current_roster(fid, cur, contracts, rosters)
    n_rows = max(cfg["roster_limit"], len(roster) + 1)
    r0, r1 = 4, 4 + n_rows - 1
    for i in range(n_rows):
        r = r0 + i
        p, c = roster[i] if i < len(roster) else (None, None)
        ws[f"B{r}"] = f"=ROW()-{r0 - 1}"
        if p:
            ws[f"C{r}"] = c["player"] if c else p["name"]
            ws[f"D{r}"] = p.get("pos") or (c or {}).get("pos", "")
            ws[f"E{r}"] = p.get("nfl", "")
        if c:
            ws[f"F{r}"] = c["draft_year"]
            ws[f"H{r}"] = "Y"
            if c["years"]:
                ws[f"I{r}"] = c["years"]
            ws[f"J{r}"] = c["price"]
        # CAP RULES (roster). Mirrors yearsOn, costIn, lastYear in app.js.
        ws[f"G{r}"] = f'=IF(F{r}="",0,SEASON-F{r}+1)'
        ws[f"K{r}"] = (f'=IF(OR(F{r}="",H{r}<>"Y"),0,IF(G{r}<=2,J{r},'
                       f'IF(I{r}>0,J{r}+STEP*(I{r}-1),0)))')
        ws[f"L{r}"] = f'=IF(OR(F{r}="",H{r}<>"Y"),0,IF(AND(I{r}="",G{r}>2),0,F{r}+1+I{r}))'
        ws[f"M{r}"] = (f'=IF(OR(F{r}="",H{r}<>"Y"),0,IF(AND(F{r}<=DRAFT_YEAR,DRAFT_YEAR<=F{r}+1+I{r}),'
                       f'IF(DRAFT_YEAR-F{r}+1<=2,J{r},J{r}+STEP*(I{r}-1)),0))')
        for col in "BCDEFGHIJKLM":
            cell = ws[f"{col}{r}"]
            inp = col in "CDEFHIJ"
            style(cell, f=font(color=INPUT_BLUE if inp else "000000"),
                  fmt=MONEY if col in "JKM" else YEAR if col in "FGIL" else None,
                  align="left" if col == "C" else "center")
            cell.protection = cell.protection.copy(locked=not inp)

    rng = lambda col: f"${col}${r0}:${col}${r1}"
    dv_keep = DataValidation(type="list", formula1='"Y,N"', allow_blank=True,
                             promptTitle="Keep?", prompt="Y keeps the player. N releases a rookie (free) or cuts a contract (dead money).")
    dv_keep.add(f"H{r0}:H{r1}")
    dv_len = DataValidation(type="whole", operator="greaterThanOrEqual", formula1="1", allow_blank=True,
                            promptTitle="Contract", prompt="Contract length in years. Only Year B players can be extended. No maximum.")
    dv_len.add(f"I{r0}:I{r1}")
    ws.add_data_validation(dv_keep)
    ws.add_data_validation(dv_len)

    # Row colors by status, first match wins.
    area = f"B{r0}:M{r1}"
    rules = [
        (f'$H{r0}="N"', None, font(color="8A8FA3", strike=True)),
        (f'$C{r0}=""', None, None),
        (f'$F{r0}=""', FILL["fa"], None),
        (f'$G{r0}=1', FILL["a"], None),
        (f'$G{r0}=2', FILL["b"], None),
        (f'AND($G{r0}>2,$I{r0}>0)', FILL["c"], None),
        (f'AND($G{r0}>2,$I{r0}="")', FILL["d"], None),
    ]
    for formula, bg, fnt in rules:
        kw = {"formula": [formula], "stopIfTrue": True}
        if bg:
            kw["fill"] = fill(bg)
        if fnt:
            kw["font"] = fnt
        ws.conditional_formatting.add(area, FormulaRule(**kw))

    # ---------- Dead contracts (O:U) ----------
    # Starts below the frozen header rows (1-3).
    ws.merge_cells("O4:U4")
    ws["O4"] = "Dead Contracts"
    ws["O4"].font = font(size=14, bold=True, color=EMBER)
    header(ws, 5, [("O", "#"), ("P", "Player"), ("Q", "Drafted"), ("R", "Contract"),
                   ("S", "Base Price"), ("T", "Owed/yr"), ("U", "Through")], bg=HULL)
    dead = [c for c in contracts if c["franchise"] == fid and c["status"] == "released"
            and c["years"] and c["dead_from"] and last_year(c) >= cur]
    nd = max(5, len(dead) + 2)
    d0, d1 = 6, 6 + nd - 1
    for i in range(nd):
        r = d0 + i
        ws[f"O{r}"] = f"=ROW()-{d0 - 1}"
        if i < len(dead):
            c = dead[i]
            ws[f"P{r}"], ws[f"Q{r}"], ws[f"R{r}"], ws[f"S{r}"] = c["player"], c["draft_year"], c["years"], c["price"]
        # CAP RULES (dead money). deadPerYear = floor(contract cost / 2) through lastYear.
        ws[f"T{r}"] = f"=IF(S{r}>0,INT((S{r}+STEP*(MAX(R{r},1)-1))/2),0)"
        ws[f"U{r}"] = f"=IF(S{r}>0,Q{r}+1+R{r},0)"
        for col in "OPQRSTU":
            cell = ws[f"{col}{r}"]
            inp = col in "PQRS"
            style(cell, f=font(color=INPUT_BLUE if inp else (EMBER if col == "T" else "000000")),
                  fmt=MONEY if col in "ST" else YEAR if col in "QRU" else None,
                  align="left" if col == "P" else "center")
            cell.protection = cell.protection.copy(locked=not inp)

    # ---------- Traded cap (O:U) ----------
    t_title = d1 + 2
    ws.merge_cells(f"O{t_title}:U{t_title}")
    ws[f"O{t_title}"] = "Traded Cap"
    ws[f"O{t_title}"].font = font(size=14, bold=True, color="2E7D4F")
    th = t_title + 1
    header(ws, th, [("O", "#"), ("P", "Trade"), ("T", "Amount"), ("U", "Draft")], bg=HULL)
    ws.merge_cells(f"P{th}:S{th}")
    tr = sorted([t for t in trades if (t["from"] == fid or t["to"] == fid) and t["draft_year"] >= cur],
                key=lambda t: t["draft_year"])
    nt = max(5, len(tr) + 2)
    t0, t1 = th + 1, th + nt
    for i in range(nt):
        r = t0 + i
        ws[f"O{r}"] = f"=ROW()-{t0 - 1}"
        ws.merge_cells(f"P{r}:S{r}")
        if i < len(tr):
            t = tr[i]
            ws[f"P{r}"] = t["note"] or f"{t['from']} trades {t['to']} ${t['amount']} in {t['draft_year']}"
            ws[f"T{r}"] = t["amount"] if t["to"] == fid else -t["amount"]
            ws[f"U{r}"] = t["draft_year"]
        for col in "OPQRSTU":
            cell = ws[f"{col}{r}"]
            inp = col in "PTU"
            style(cell, f=font(color=INPUT_BLUE if inp else "000000"),
                  fmt=MONEY_SIGNED if col == "T" else YEAR if col == "U" else None,
                  align="left" if col == "P" else "center")
            cell.protection = cell.protection.copy(locked=not inp)
    ws[f"T{t1 + 1}"] = "+ received, − sent"
    ws[f"T{t1 + 1}"].font = font(size=9, italic=True, color="6B7290")

    # ---------- Summary (E:J) and legend (B:C) ----------
    s0 = r1 + 2
    header(ws, s0, [("E", "Cap"), ("G", None), ("I", None)])
    ws.merge_cells(f"E{s0}:F{s0}")
    ws.merge_cells(f"G{s0}:H{s0}")
    ws.merge_cells(f"I{s0}:J{s0}")
    ws[f"G{s0}"] = '=SEASON&" season"'
    ws[f"I{s0}"] = '=DRAFT_YEAR&" draft"'

    def dead_in(y):
        # Existing dead contracts + planned cuts (Keep? = N on a contract player).
        return (f"SUMPRODUCT(($U${d0}:$U${d1}>={y})*$T${d0}:$T${d1})"
                f"+SUMPRODUCT(({rng('H')}=\"N\")*({rng('G')}>2)*({rng('I')}>0)*({rng('F')}+1+{rng('I')}>={y})"
                f"*INT(({rng('J')}+STEP*({rng('I')}-1))/2))")

    lines = [
        ("Salary cap", "=CAP", "=CAP", MONEY),
        ("Salary", f"=SUM({rng('K')})", f"=SUM({rng('M')})", MONEY),
        ("Dead money", "=" + dead_in("SEASON"), "=" + dead_in("DRAFT_YEAR"), MONEY),
        ("Traded cap", f"=SUMIF($U${t0}:$U${t1},SEASON,$T${t0}:$T${t1})",
         f"=SUMIF($U${t0}:$U${t1},DRAFT_YEAR,$T${t0}:$T${t1})", MONEY_SIGNED),
        ("Available", None, None, MONEY),
        ("Players kept", f'=COUNTIF({rng("K")},">0")', f'=COUNTIF({rng("M")},">0")', "0"),
        ("To decide", "", f'=SUMPRODUCT(({rng("H")}="Y")*({rng("I")}="")*({rng("F")}=DRAFT_YEAR-2))', "0"),
    ]
    refs = {}
    for i, (label, a, b, fmt) in enumerate(lines):
        r = s0 + 1 + i
        if label == "Available":
            a = f"=G{r-4}-G{r-3}-G{r-2}+G{r-1}"
            b = f"=I{r-4}-I{r-3}-I{r-2}+I{r-1}"
        ws.merge_cells(f"E{r}:F{r}")
        ws.merge_cells(f"G{r}:H{r}")
        ws.merge_cells(f"I{r}:J{r}")
        ws[f"E{r}"], ws[f"G{r}"], ws[f"I{r}"] = label, a, b
        bold = label == "Available"
        style(ws[f"E{r}"], f=font(bold=True, color="FFFFFF"), bg=NAVY if not bold else HULL)
        for col in "GI":
            style(ws[f"{col}{r}"], f=font(bold=bold, size=12 if bold else 11), fmt=fmt, align="center")
        for col in "FHJ":
            ws[f"{col}{r}"].border = BOX
        refs[label] = (f"G{r}", f"I{r}")
    avail = s0 + 5
    for col in "GI":
        cell = f"{col}{avail}"
        ws.conditional_formatting.add(cell, FormulaRule(formula=[f"{cell}<0"], font=font(bold=True, size=12, color=EMBER)))
        ws.conditional_formatting.add(cell, FormulaRule(formula=[f"{cell}>=0"], font=font(bold=True, size=12, color="2E7D4F")))
    ws[f"I{s0 + 7}"].comment = Comment("Year B players with no contract entered. Their cost is not in the draft salary until you enter a contract length.", "League")

    legend = [("Contract", FILL["c"]), ("Year B", FILL["b"]), ("Year A", FILL["a"]),
              ("Extend or release", FILL["d"]), ("Free agent ($0)", FILL["fa"])]
    for i, (label, bg) in enumerate(legend):
        r = s0 + i
        ws[f"C{r}"] = label
        ws.merge_cells(f"C{r}:D{r}")
        style(ws[f"C{r}"], f=font(bold=True), bg=bg)
        ws[f"D{r}"].border = BOX
    r = s0 + len(legend)
    ws.merge_cells(f"C{r}:D{r}")
    ws[f"C{r}"] = "Cut / released"
    style(ws[f"C{r}"], f=font(color="8A8FA3", strike=True))
    ws[f"D{r}"].border = BOX
    r += 1
    ws.merge_cells(f"C{r}:D{r}")
    ws[f"C{r}"] = "Blue text = editable"
    style(ws[f"C{r}"], f=font(color=INPUT_BLUE, bold=True))
    ws[f"D{r}"].border = BOX

    ws.freeze_panes = "C4"
    ws.protection.sheet = True  # no password; Review > Unprotect Sheet to change anything
    ws.protection.formatCells = False
    ws.protection.formatColumns = False
    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToWidth = 1
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    return refs


def build_info(ws, cfg, cur, nxt, team_refs, records, stamp):
    ws.sheet_view.showGridLines = False
    for k, v in {"A": 2, "B": 26, "C": 28, "D": 9, "E": 11, "F": 11, "G": 11, "H": 3,
                 "I": 9, "J": 11, "K": 11, "L": 11, "M": 12, "N": 10}.items():
        ws.column_dimensions[k].width = v
    ws.merge_cells("B2:N2")
    ws["B2"] = cfg["league_name"]
    ws["B2"].font = font(size=20, bold=True, color=NAVY)
    ws["B3"] = "League info and cap summary. Each team sheet reads the settings below."
    ws["B3"].font = font(italic=True, color="6B7290")

    settings = [
        ("Current season", cur, "0", "Season the rosters are from. The Year column on every team sheet counts from this."),
        ("Draft year", nxt, "0", "Next auction draft. Change it to look further ahead."),
        ("Salary cap", cfg["salary_cap"], MONEY, "Enforced only at the auction draft."),
        ("Extension step", cfg["extension_step"], MONEY, "Contract cost = base price + step × (years − 1)."),
        ("Roster limit", cfg["roster_limit"], "0", None),
        ("Data as of", f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:]}", "@", "Rebuilt whenever rosters, contracts, or cap trades change."),
    ]
    for i, (label, val, fmt, note) in enumerate(settings):
        r = 5 + i
        ws[f"B{r}"], ws[f"C{r}"] = label, val
        style(ws[f"B{r}"], f=font(bold=True, color="FFFFFF"), bg=NAVY)
        editable = i < 2
        style(ws[f"C{r}"], f=font(bold=True, color=INPUT_BLUE if editable else "000000"), fmt=fmt, align="center")
        ws[f"C{r}"].protection = ws[f"C{r}"].protection.copy(locked=not editable)
        if note:
            ws[f"D{r}"] = note
            ws[f"D{r}"].font = font(italic=True, size=10, color="6B7290")

    # Team table
    h = 13
    cols = [("B", "Team"), ("C", "Owners"), ("D", "Record"),
            ("E", '=SEASON&" salary"'), ("F", '=SEASON&" dead"'), ("G", '=SEASON&" space"'),
            ("I", '=DRAFT_YEAR&" kept"'), ("J", "Salary"), ("K", "Dead money"), ("L", "Traded cap"),
            ("M", "Draft budget"), ("N", "To decide")]
    header(ws, h, cols)
    ws.row_dimensions[h].height = 30
    for col, _ in cols:
        ws[f"{col}{h}"].alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    ws.merge_cells(f"I{h - 1}:N{h - 1}")
    ws[f"I{h - 1}"] = '="Next draft: "&DRAFT_YEAR'
    ws.merge_cells(f"D{h - 1}:G{h - 1}")
    ws[f"D{h - 1}"] = '="This season: "&SEASON'
    for c in (f"I{h - 1}", f"D{h - 1}"):
        ws[c].font = font(bold=True, color=NAVY)
        ws[c].alignment = Alignment(horizontal="center")

    for i, (f, sname, refs) in enumerate(team_refs):
        r = h + 1 + i
        q = f"'{sname}'!"
        rec = records.get(f["id"])
        ws[f"B{r}"] = f'=HYPERLINK("#{q}A1","{f["team_name"]}")'
        ws[f"C{r}"] = f["owners"]
        ws[f"D{r}"] = f"{rec['wins']}-{rec['losses']}" + (f"-{rec['ties']}" if rec.get("ties") else "") if rec else ""
        ws[f"E{r}"] = f"={q}{refs['Salary'][0]}"
        ws[f"F{r}"] = f"={q}{refs['Dead money'][0]}"
        ws[f"G{r}"] = f"={q}{refs['Available'][0]}"
        ws[f"I{r}"] = f"={q}{refs['Players kept'][1]}"
        ws[f"J{r}"] = f"={q}{refs['Salary'][1]}"
        ws[f"K{r}"] = f"={q}{refs['Dead money'][1]}"
        ws[f"L{r}"] = f"={q}{refs['Traded cap'][1]}"
        ws[f"M{r}"] = f"={q}{refs['Available'][1]}"
        ws[f"N{r}"] = f"={q}{refs['To decide'][1]}"
        stripe = "F3F4F8" if i % 2 else None
        for col in "BCDEFGIJKLMN":
            fmt = MONEY_SIGNED if col == "L" else MONEY if col in "EFGJKM" else "0" if col in "IN" else None
            style(ws[f"{col}{r}"], f=font(bold=col in "BM", color="1F4FD8" if col == "B" else "000000",
                                          underline="single" if col == "B" else None),
                  bg=stripe, fmt=fmt, align="left" if col in "BC" else "center")
        for col in "GM":
            cell = f"{col}{r}"
            ws.conditional_formatting.add(cell, FormulaRule(formula=[f"{cell}<0"], font=font(bold=True, color=EMBER)))
    last = h + len(team_refs)
    r = last + 2
    notes = [
        "How to use: on any team sheet, edit the blue cells.",
        "Keep? = N releases a rookie (free) or cuts a contract (dead money from this season through the contract's last year).",
        "Enter a Contract length on a Year B player to extend him. His new cost starts at the next draft.",
        "Add rows to Dead Contracts or Traded Cap to test trades. Formula cells are locked; Review > Unprotect Sheet to change them.",
        "Same rules as starwarsdynasty.github.io. The website is the official record.",
    ]
    for i, t in enumerate(notes):
        ws[f"B{r + i}"] = t
        ws[f"B{r + i}"].font = font(size=10, color="3A4166", bold=i == 0)
    ws.freeze_panes = f"A{h + 1}"
    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToWidth = 1
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.protection.sheet = True
    ws.protection.formatColumns = False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="rebuild even if nothing changed")
    a = ap.parse_args()
    h = input_hash()
    old = json.loads(META.read_text()) if META.exists() else {}
    if not a.force and old.get("hash") == h and XLSX.exists():
        print("Workbook up to date.")
        return
    stamp = datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y%m%d")
    print(build(stamp))
    META.write_text(json.dumps({"built": stamp, "hash": h}, indent=2) + "\n")


if __name__ == "__main__":
    main()
