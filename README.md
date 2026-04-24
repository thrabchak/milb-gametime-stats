# milb-gametime-stats

Fetches rosters and current season stats for a MiLB game via the public MLB Stats API and writes them to a Google Sheet.

## What it does

Given a game, the script:
- Prints both teams' rosters and stats to the console
- Writes the same data to a configured Google Sheet with a **stable, fixed layout** so cell addresses never change between runs

Stats shown:
- **Batters:** AVG, HR, RBI, OPS, SB
- **Pitchers:** ERA, W-L, IP, K, WHIP

For scheduled (pre-game) games, stats are fetched directly from the MLB Stats API since the boxscore doesn't populate them until the game starts.

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Enable the Google Sheets API

Follow the [Python quickstart](https://developers.google.com/workspace/sheets/api/quickstart/python) to:
1. Create a Google Cloud project
2. Enable the Google Sheets API
3. Create OAuth 2.0 credentials and download them as `credentials.json` in the project root

### 3. Authenticate

On first run the script will open a browser to complete OAuth. It saves the token to `token.json` for subsequent runs. The script requires **read/write** scope (`https://www.googleapis.com/auth/spreadsheets`) — if you previously ran the Google Sheets quickstart, the old `token.json` will be automatically replaced.

## Usage

```bash
# List today's Triple-A games and pick one interactively
python main.py

# List today's games at a specific level (AAA, AA, A+, A, R)
python main.py AA

# Jump straight to a game by its MLB game PK
python main.py 816595

# Game PK at a specific level
python main.py 816595 AA
```

Game PKs are shown in the schedule listing. You can also find them on [MLB.com](https://www.mlb.com) or via the [MLB Stats API](https://statsapi.mlb.com/api/v1/schedule).

## Google Sheet

Data is written to [this sheet](https://docs.google.com/spreadsheets/d/1ta8zudzUeu6srDFbuSgstAcrjPLgLobnT7snMDEuAkg). The sheet is cleared and rewritten on each run.

### Layout

```
Row 1:  Game <PK>  |  <Away> @ <Home>  |  <Date>  |  <Status>
Row 2:  (blank)
Rows 3–6:   SCORE section
Row 7:  (blank)
Rows 8–90:  Away team sections
Rows 91–92: (blank)
Rows 93–175: Home team sections
```

### Score section (4 rows, always rows 3–6)

```
SCORE
      Team                    R   H   E
Away  <away team name>        #   #   #
Home  <home team name>        #   #   #
```

### Per-team sections (83 rows each, fixed)

Each team block contains four sections. Row offsets are relative to the start of the team block.

| Rows | Section | Columns |
|------|---------|---------|
| 1 | Team name | |
| 2 | *(blank)* | |
| 3 | `BATTING ORDER` label | |
| 4 | Headers | `#  POS  Jersey  Name  AVG  HR  RBI  OPS  SB` |
| 5–13 | Batting order (9 rows, padded with blanks if lineup not yet submitted) | |
| 14 | *(blank)* | |
| 15 | `CURRENT PITCHER` label | |
| 16 | Headers | `Role  Jersey  Name  ERA  W-L  IP  K  WHIP` |
| 17 | Current pitcher (blank pre-game) | |
| 18 | *(blank)* | |
| 19 | `PITCHING STAFF` label | |
| 20 | Headers | `Role  Jersey  Name  ERA  W-L  IP  K  WHIP` |
| 21–50 | Pitching staff (30 rows, padded) | |
| 51 | *(blank)* | |
| 52 | `HITTERS` label | |
| 53 | Headers | `POS  Jersey  Name  AVG  HR  RBI  OPS  SB` |
| 54–83 | All hitters (30 rows, padded) | |

Because every section has a fixed row count, you can write Google Sheets formulas or conditional formatting that reference specific rows and they will remain valid whether the script is run before the game, during it, or after.

## Files

| File | Description |
|------|-------------|
| `main.py` | Main script |
| `credentials.json` | OAuth client secrets (not committed) |
| `token.json` | Cached OAuth token (not committed) |
| `google-sheets-quickstart.py` | Original Google Sheets API quickstart reference |
