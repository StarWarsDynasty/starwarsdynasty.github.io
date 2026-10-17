# The Dynasty Awakens

League site for the Star Wars dynasty fantasy football league. Served by GitHub Pages at https://starwarsdynasty.github.io.

The site is static. It reads the files in `data/` and computes every salary, budget, and dead-money figure in the browser. A nightly GitHub Action pulls rosters from ESPN.

## Files you edit

All in `data/`. Edit them in the GitHub web editor (open the file, click the pencil) or locally.

| File | What it holds | When you edit it |
|---|---|---|
| `contracts.csv` | One row per drafted player | Extensions, fixes |
| `cap_trades.csv` | Cap money traded between teams | When a cap trade is reported |
| `seasons.csv` | Final place and record for each franchise each season | After the season ends |
| `franchises.csv` | Owners (team names come from ESPN nightly) | Owner changes |
| `config.json` | Current season, cap, extension step | Once a year |

### contracts.csv

| Column | Meaning |
|---|---|
| `player` | Name. Matching uses `espn_id` once it is filled. |
| `franchise` | Franchise id (see `franchises.csv`). The sync updates this after trades. |
| `draft_year` | Season the player was drafted. |
| `price` | Winning auction bid. |
| `years` | Extension length. Blank while on the rookie deal. |
| `status` | `active`, `released`, or `expired`. |
| `dead_from` | Season of the cut, when dead money starts. Set by the sync on release. |
| `espn_id` | Filled by the sync. |

**Extending a player:** set `years`. Example: Bhayshul Tuten, price 9, extended 3 years, gives a cost of 9 + 5 × 2 = $19 through 2029.

**Releasing a player by hand:** set `status` to `released`. If he had an extension, set `dead_from` to the season he was cut.

### cap_trades.csv

```
draft_year,from,to,amount,note
2027,renbarger,b_mcmillan,35,Renbarger trades B. McMillan $35 in 2027
```

`from` pays, `to` receives. Use franchise ids.

### seasons.csv

Add 12 rows after each season. `place` is a number. `mark` is `*` or `**` for the footnotes in `config.json`, otherwise blank.

## What the nightly sync does

`scripts/fetch_espn.py`, run by `.github/workflows/espn-sync.yml` daily and on demand (Actions tab → ESPN sync → Run workflow).

- Saves current rosters and records to `data/espn/rosters.json`.
- Fills `espn_id` and `espn_team_id`.
- Trade (ESPN acquisition type TRADE): moves the contract to the new team.
- Drop: marks the contract `released`. Extended players owe dead money from the season of the cut through the contract's last year.
- After the auction: adds a contract row for every non-keeper pick, at the winning bid.
- Writes problems to `data/espn/report.json` and every change to `data/events.csv`.

Releases are only detected by comparing to the previous ESPN snapshot, so the first run never releases anyone.

## Secrets

Settings → Secrets and variables → Actions:

- `ESPN_S2`
- `ESPN_SWID` (include the braces)

If the sync fails with a 401 or 403, the cookies expired. Copy fresh ones from Firefox (Web Developer Tools → Storage → Cookies → espn.com) and update both secrets.

## New season checklist

1. Add last season's final standings to `seasons.csv`.
2. Update owners in `franchises.csv` if anyone changed. Team names update from ESPN.
3. Enter extensions in `contracts.csv`.
4. Set `current_season` in `config.json`.
5. Run the sync. Expired contracts are marked automatically.
6. Use the Salary cap page to enter keeper values in ESPN.

## Planning mode

Each team page has a **Plan moves** button. Owners can try extensions, cuts, and releases and see the effect on every future draft. Nothing is saved. The scenario lives in the URL (`#/team/turner/plan/...`), so **Copy link to this plan** gives a link to share in Discord.
