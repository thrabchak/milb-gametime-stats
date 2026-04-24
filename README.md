# milb-gametime-stats

Fetches rosters and current season stats for a MiLB game via the public MLB Stats API and writes them to a Google Sheet.

## What it does

Given a game, the script:
- Prints both teams' batting orders, pitching staffs, and bench players to the console
- Writes the same data — with stats — to a configured Google Sheet

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

Data is written to [this sheet](https://docs.google.com/spreadsheets/d/1ta8zudzUeu6srDFbuSgstAcrjPLgLobnT7snMDEuAkg). The sheet is cleared and rewritten each run. Layout:

```
Game <PK>  <Away> @ <Home>  <Date>  <Status>

AWAY TEAM
#  POS  Name  AVG  HR  RBI  OPS  SB
...batting order rows...

Role  Name  ERA  W-L  IP  K  WHIP
...pitcher rows...

   POS  Name  AVG  HR  RBI  OPS  SB
...bench rows...


HOME TEAM
...same structure...
```

## Files

| File | Description |
|------|-------------|
| `main.py` | Main script |
| `credentials.json` | OAuth client secrets (not committed) |
| `token.json` | Cached OAuth token (not committed) |
| `google-sheets-quickstart.py` | Original Google Sheets API quickstart reference |
