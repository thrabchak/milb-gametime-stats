"""
MiLB Game Time Stats
Fetches both teams' rosters and current season stats for a minor league game
via the public MLB Stats API, then writes the data to a Google Sheet.

Usage:
    python main.py                   # lists today's AAA games, prompts for selection
    python main.py AA                # lists today's AA games instead
    python main.py 816595            # jump straight to a specific game by PK
    python main.py 816595 AA         # game PK at a specific level

The script writes to:
    https://docs.google.com/spreadsheets/d/1ta8zudzUeu6srDFbuSgstAcrjPLgLobnT7snMDEuAkg
"""

import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# ── Constants ─────────────────────────────────────────────────────────────────

BASE_URL = "https://statsapi.mlb.com/api/v1"

# MLB Stats API sport IDs for each MiLB level
LEVELS = {"AAA": 11, "AA": 12, "A+": 13, "A": 14, "R": 16}

SPREADSHEET_ID = "1ta8zudzUeu6srDFbuSgstAcrjPLgLobnT7snMDEuAkg"

# Full read/write scope required; if token.json has only readonly it will be replaced.
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Fixed row counts per section — keep these constant so each player's cell
# address never changes between runs, regardless of game state.
LINEUP_SIZE = 9
CURRENT_PITCHER_SIZE = 1
PITCHING_STAFF_SIZE = 30
HITTER_SIZE = 30


# ── MLB Stats API ──────────────────────────────────────────────────────────────

def api_get(path, params=None):
    """Make a GET request to the MLB Stats API and return parsed JSON."""
    resp = requests.get(f"{BASE_URL}{path}", params=params, timeout=10)
    resp.raise_for_status()
    return resp.json()


def fetch_games(date_str, sport_id=11):
    """Return a list of games scheduled for the given date and MiLB level.

    Args:
        date_str: Date in 'YYYY-MM-DD' format.
        sport_id: MLB Stats API sport ID (default 11 = Triple-A).

    Returns:
        List of dicts with keys: gamePk, away, home, status, venue.
    """
    data = api_get("/schedule", {
        "sportId": sport_id,
        "date": date_str,
        "hydrate": "team,venue,status",
    })
    games = []
    for date_entry in data.get("dates", []):
        for g in date_entry.get("games", []):
            games.append({
                "gamePk": g["gamePk"],
                "away": g["teams"]["away"]["team"]["name"],
                "home": g["teams"]["home"]["team"]["name"],
                "status": g["status"]["detailedState"],
                "venue": g.get("venue", {}).get("name", ""),
                "date": date_entry["date"],
            })
    return games


def fetch_game_summary(game_pk):
    """Fetch basic game metadata (teams, date, status) for a given game PK.

    Used when the user passes a game PK directly and we don't have the info
    from a prior schedule fetch.

    Returns:
        Dict with keys: gamePk, away, home, status, venue, date.
    """
    data = api_get("/schedule", {
        "gamePk": game_pk,
        "hydrate": "team,venue,status",
    })
    for date_entry in data.get("dates", []):
        for g in date_entry.get("games", []):
            return {
                "gamePk": g["gamePk"],
                "away": g["teams"]["away"]["team"]["name"],
                "home": g["teams"]["home"]["team"]["name"],
                "status": g["status"]["detailedState"],
                "venue": g.get("venue", {}).get("name", ""),
                "date": date_entry["date"],
            }
    return {"gamePk": game_pk, "away": "Away", "home": "Home", "status": "", "venue": "", "date": ""}


def fetch_boxscore(game_pk):
    """Fetch the full boxscore for a game, including rosters and season stats.

    Args:
        game_pk: Unique MLB game identifier.

    Returns:
        Boxscore dict as returned by the API.
    """
    return api_get(f"/game/{game_pk}/boxscore")


def _boxscore_has_stats(boxscore):
    """Return True if any player in the boxscore has populated seasonStats.

    The API leaves seasonStats all-zero for scheduled (pre-game) games, so
    this is how we detect whether a separate stats fetch is needed.
    """
    for side in ("away", "home"):
        for p in boxscore["teams"][side]["players"].values():
            if p.get("seasonStats", {}).get("batting", {}).get("gamesPlayed", 0) > 0:
                return True
    return False


def _fetch_stats_for_player(person_id, season, sport_id):
    """Fetch hitting and pitching season stats for a single player.

    Returns:
        Tuple of (person_id, {"hitting": {...}, "pitching": {...}}).
    """
    data = api_get(f"/people/{person_id}/stats", {
        "stats": "season",
        "season": season,
        "sportId": sport_id,
        "group": "hitting,pitching",
    })
    result = {}
    for sg in data.get("stats", []):
        group = sg.get("group", {}).get("displayName", "").lower()
        splits = sg.get("splits", [])
        if splits:
            result[group] = splits[0]["stat"]
    return person_id, result


def backfill_stats(boxscore, season):
    """Fetch season stats from the API and inject them into the boxscore.

    Called when the boxscore's embedded seasonStats are empty (pre-game).
    Fetches all players for both teams concurrently, then writes hitting and
    pitching stats back into each player's seasonStats dict in place.

    Args:
        boxscore: Boxscore dict, mutated in place.
        season: Four-digit season year as an int or str.
    """
    sport_id = boxscore["teams"]["away"]["team"]["sport"]["id"]

    # Collect every player ID across both teams
    all_players = {}
    for side in ("away", "home"):
        for key, p in boxscore["teams"][side]["players"].items():
            pid = p["person"]["id"]
            all_players[pid] = (side, key)

    print(f"  Fetching stats for {len(all_players)} players...", flush=True)

    stats_map = {}
    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = {
            pool.submit(_fetch_stats_for_player, pid, season, sport_id): pid
            for pid in all_players
        }
        for future in as_completed(futures):
            pid, stats = future.result()
            stats_map[pid] = stats

    # Inject fetched stats back into the boxscore
    for side in ("away", "home"):
        for p in boxscore["teams"][side]["players"].values():
            pid = p["person"]["id"]
            fetched = stats_map.get(pid, {})
            if "hitting" in fetched:
                p.setdefault("seasonStats", {})["batting"] = fetched["hitting"]
            if "pitching" in fetched:
                p.setdefault("seasonStats", {})["pitching"] = fetched["pitching"]


# ── Formatting helpers ─────────────────────────────────────────────────────────

def fmt_hitting(s):
    """Format a batting seasonStats dict into a readable console stat line."""
    if not s or s.get("gamesPlayed", 0) == 0:
        return "no stats yet"
    return (
        f"AVG {s.get('avg', '---')}  "
        f"HR {s.get('homeRuns', 0):>2}  "
        f"RBI {s.get('rbi', 0):>3}  "
        f"OPS {s.get('ops', '---')}  "
        f"SB {s.get('stolenBases', 0)}"
    )


def fmt_pitching(s):
    """Format a pitching seasonStats dict into a readable console stat line."""
    if not s or float(s.get("inningsPitched", 0)) == 0.0:
        return "no stats yet"
    return (
        f"ERA {s.get('era', '---')}  "
        f"{s.get('wins', 0)}-{s.get('losses', 0)}  "
        f"IP {s.get('inningsPitched', '0.0'):>5}  "
        f"K {s.get('strikeOuts', 0):>3}  "
        f"WHIP {s.get('whip', '---')}"
    )


# ── Console display ────────────────────────────────────────────────────────────

def display_roster(side, boxscore):
    """Print the roster and season stats for one side of a boxscore.

    Sections printed: batting order (or all non-pitchers if lineup not yet
    submitted), pitching staff, and bench.

    Args:
        side: 'away' or 'home'.
        boxscore: Full boxscore dict from fetch_boxscore().
    """
    team_data = boxscore["teams"][side]
    team_name = team_data["team"]["name"]
    players = team_data["players"]
    batting_order = team_data.get("battingOrder", [])
    pitchers = team_data.get("pitchers", [])
    bullpen = team_data.get("bullpen", [])
    bench = team_data.get("bench", [])

    print(f"\n{'=' * 72}")
    print(f"  {team_name.upper()}")
    print(f"{'=' * 72}")

    all_hitter_ids = list(batting_order) + [p for p in bench if p not in set(batting_order)]
    all_pitcher_ids = pitchers + bullpen
    current_pitcher_id = pitchers[-1] if pitchers else None

    # --- BATTING ORDER ---
    print(f"\n  BATTING ORDER")
    print(f"  {'#':>2}  {'POS':<4}  {'#':>3}  {'NAME':<27}  SEASON STATS")
    print(f"  {'-' * 72}")
    if batting_order:
        for i, pid in enumerate(batting_order):
            p = players.get(f"ID{pid}", {})
            pos = p.get("position", {}).get("abbreviation", "?")
            jersey = p.get("jerseyNumber", "")
            name = p.get("person", {}).get("fullName", "Unknown")
            hitting = p.get("seasonStats", {}).get("batting", {})
            print(f"  {i + 1:>2}  {pos:<4}  {jersey:>3}  {name:<27}  {fmt_hitting(hitting)}")
    else:
        print(f"  {'':>2}  {'':4}  {'':3}  (lineup not yet submitted)")

    # --- CURRENT PITCHER ---
    print(f"\n  CURRENT PITCHER")
    print(f"  {'ROLE':<5}  {'#':>3}  {'NAME':<29}  SEASON STATS")
    print(f"  {'-' * 72}")
    if current_pitcher_id:
        p = players.get(f"ID{current_pitcher_id}", {})
        jersey = p.get("jerseyNumber", "")
        name = p.get("person", {}).get("fullName", "Unknown")
        pitching = p.get("seasonStats", {}).get("pitching", {})
        role = "SP" if pitchers[0] == current_pitcher_id else "RP"
        print(f"  {role:<5}  {jersey:>3}  {name:<29}  {fmt_pitching(pitching)}")
    else:
        print(f"  {'':5}  {'':3}  (pre-game)")

    # --- PITCHING STAFF ---
    print(f"\n  PITCHING STAFF")
    print(f"  {'ROLE':<5}  {'#':>3}  {'NAME':<29}  SEASON STATS")
    print(f"  {'-' * 72}")
    for i, pid in enumerate(all_pitcher_ids):
        p = players.get(f"ID{pid}", {})
        jersey = p.get("jerseyNumber", "")
        name = p.get("person", {}).get("fullName", "Unknown")
        pitching = p.get("seasonStats", {}).get("pitching", {})
        # Only label SP if we haven't tracked in-game pitchers yet;
        # once pitchers list is non-empty we know who actually started.
        role = "SP" if i == 0 else "RP"
        print(f"  {role:<5}  {jersey:>3}  {name:<29}  {fmt_pitching(pitching)}")

    # --- HITTERS ---
    print(f"\n  HITTERS")
    print(f"  {'':>2}  {'POS':<4}  {'#':>3}  {'NAME':<27}  SEASON STATS")
    print(f"  {'-' * 72}")
    for pid in all_hitter_ids:
        p = players.get(f"ID{pid}", {})
        pos = p.get("position", {}).get("abbreviation", "?")
        jersey = p.get("jerseyNumber", "")
        name = p.get("person", {}).get("fullName", "Unknown")
        hitting = p.get("seasonStats", {}).get("batting", {})
        print(f"  {'':>2}  {pos:<4}  {jersey:>3}  {name:<27}  {fmt_hitting(hitting)}")


# ── Google Sheets ──────────────────────────────────────────────────────────────

def get_sheets_service():
    """Authenticate with Google and return a Sheets API service client.

    Reads credentials from token.json if present. If the stored token lacks
    write scope (e.g. was created by the read-only quickstart), it is deleted
    and the OAuth flow runs again to obtain a write-capable token.

    Returns:
        Authenticated googleapiclient Resource for the Sheets v4 API.
    """
    creds = None
    if os.path.exists("token.json"):
        # Read the raw JSON to check stored scopes — creds.scopes reflects the
        # scopes passed to from_authorized_user_file, not what's in the file.
        with open("token.json") as f:
            token_data = json.load(f)
        if SCOPES[0] in token_data.get("scopes", []):
            creds = Credentials.from_authorized_user_file("token.json", SCOPES)
        else:
            os.remove("token.json")

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
            creds = flow.run_local_server(port=0)
        with open("token.json", "w") as f:
            f.write(creds.to_json())

    return build("sheets", "v4", credentials=creds)


def _hitting_row(order, p):
    """Build a single spreadsheet row for a batter."""
    s = p.get("seasonStats", {}).get("batting", {})
    pos = p.get("position", {}).get("abbreviation", "?")
    jersey = p.get("jerseyNumber", "")
    name = p.get("person", {}).get("fullName", "Unknown")
    if not s or s.get("gamesPlayed", 0) == 0:
        return [order, pos, jersey, name, "", "", "", "", ""]
    return [
        order, pos, jersey, name,
        s.get("avg", ""),
        s.get("homeRuns", ""),
        s.get("rbi", ""),
        s.get("ops", ""),
        s.get("stolenBases", ""),
    ]


def _pitching_row(role, p):
    """Build a single spreadsheet row for a pitcher."""
    s = p.get("seasonStats", {}).get("pitching", {})
    jersey = p.get("jerseyNumber", "")
    name = p.get("person", {}).get("fullName", "Unknown")
    no_stats = not s or float(s.get("inningsPitched", 0)) == 0.0
    if no_stats:
        return [role, jersey, name, "", "", "", "", ""]
    return [
        role, jersey, name,
        s.get("era", ""),
        f"{s.get('wins', 0)}-{s.get('losses', 0)}",
        s.get("inningsPitched", ""),
        s.get("strikeOuts", ""),
        s.get("whip", ""),
    ]


def build_score_rows(boxscore):
    """Build the score section for the sheet (always exactly 4 rows).

    Layout:
        SCORE label               1 row
        Column headers            1 row
        Away team score row       1 row
        Home team score row       1 row

    Args:
        boxscore: Full boxscore dict from fetch_boxscore().

    Returns:
        List of 4 lists.
    """
    rows = [["SCORE"], ["", "Team", "R", "H", "E"]]
    for side, label in (("away", "Away"), ("home", "Home")):
        t = boxscore["teams"][side]
        batting = t.get("teamStats", {}).get("batting", {})
        fielding = t.get("teamStats", {}).get("fielding", {})
        rows.append([
            label,
            t["team"]["name"],
            batting.get("runs", 0),
            batting.get("hits", 0),
            fielding.get("errors", 0),
        ])
    return rows  # always 4 rows


def build_team_rows(side, boxscore):
    """Build a 2-D list of spreadsheet rows for one team with fixed section sizes.

    Every section always occupies the same number of rows so that cell addresses
    never shift between runs, regardless of game state (pre-game, live, final):

        Team name                           1 row
        (blank)                             1 row
        BATTING ORDER label + headers       2 rows
        Batting order data                  LINEUP_SIZE rows (padded with blanks)
        (blank)                             1 row
        CURRENT PITCHER label + headers     2 rows
        Current pitcher data                1 row  (CURRENT_PITCHER_SIZE)
        (blank)                             1 row
        PITCHING STAFF label + headers      2 rows
        Pitching staff data                 PITCHING_STAFF_SIZE rows (padded)
        (blank)                             1 row
        HITTERS label + headers             2 rows
        Hitter data                         HITTER_SIZE rows (padded)

    Args:
        side: 'away' or 'home'.
        boxscore: Full boxscore dict from fetch_boxscore().

    Returns:
        List of lists (rows × columns).
    """
    team_data = boxscore["teams"][side]
    team_name = team_data["team"]["name"]
    players = team_data["players"]
    batting_order = team_data.get("battingOrder", [])
    pitchers = team_data.get("pitchers", [])
    bullpen = team_data.get("bullpen", [])
    bench = team_data.get("bench", [])

    # All hitters: official batting order (if known) followed by bench.
    # Pre-game, batting_order is empty and bench holds the full position player
    # roster, so this always produces the complete hitter list either way.
    all_hitter_ids = list(batting_order) + [p for p in bench if p not in set(batting_order)]

    # Pitching staff: pitchers who've appeared, then unused bullpen.
    all_pitcher_ids = pitchers + bullpen

    # Current pitcher: last to enter the game; blank pre-game.
    current_pitcher_id = pitchers[-1] if pitchers else None

    rows = []
    rows.append([team_name.upper()])
    rows.append([])

    # ── BATTING ORDER ──────────────────────────────────────────────────────────
    rows.append(["BATTING ORDER"])
    rows.append(["#", "POS", "Jersey", "Name", "AVG", "HR", "RBI", "OPS", "SB"])
    for i in range(LINEUP_SIZE):
        if i < len(batting_order):
            p = players.get(f"ID{batting_order[i]}", {})
            rows.append(_hitting_row(str(i + 1), p))
        else:
            rows.append(["", "", "", "", "", "", "", "", ""])
    rows.append([])

    # ── CURRENT PITCHER ────────────────────────────────────────────────────────
    rows.append(["CURRENT PITCHER"])
    rows.append(["Role", "Jersey", "Name", "ERA", "W-L", "IP", "K", "WHIP"])
    if current_pitcher_id:
        p = players.get(f"ID{current_pitcher_id}", {})
        role = "SP" if pitchers[0] == current_pitcher_id else "RP"
        rows.append(_pitching_row(role, p))
    else:
        rows.append(["", "", "", "", "", "", "", ""])
    rows.append([])

    # ── PITCHING STAFF ─────────────────────────────────────────────────────────
    rows.append(["PITCHING STAFF"])
    rows.append(["Role", "Jersey", "Name", "ERA", "W-L", "IP", "K", "WHIP"])
    for i in range(PITCHING_STAFF_SIZE):
        if i < len(all_pitcher_ids):
            p = players.get(f"ID{all_pitcher_ids[i]}", {})
            role = "SP" if i == 0 else "RP"
            rows.append(_pitching_row(role, p))
        else:
            rows.append(["", "", "", "", "", "", "", ""])
    rows.append([])

    # ── HITTERS ────────────────────────────────────────────────────────────────
    rows.append(["HITTERS"])
    rows.append(["", "POS", "Jersey", "Name", "AVG", "HR", "RBI", "OPS", "SB"])
    for i in range(HITTER_SIZE):
        if i < len(all_hitter_ids):
            p = players.get(f"ID{all_hitter_ids[i]}", {})
            rows.append(_hitting_row("", p))
        else:
            rows.append(["", "", "", "", "", "", "", ""])

    return rows


def write_to_sheet(service, game_info, boxscore):
    """Clear Sheet1 and write game info plus both teams' rosters and stats.

    Layout:
        Game header row (PK, matchup, date, status)   1 row
        (blank)                                        1 row
        Score section                                  4 rows  (build_score_rows)
        (blank)                                        1 row
        Away team section                              build_team_rows
        (two blank rows)
        Home team section                              build_team_rows

    Args:
        service: Authenticated Sheets API service from get_sheets_service().
        game_info: Dict with keys gamePk, away, home, date, status.
        boxscore: Full boxscore dict from fetch_boxscore().
    """
    score_rows = build_score_rows(boxscore)
    away_rows = build_team_rows("away", boxscore)
    home_rows = build_team_rows("home", boxscore)

    all_rows = [
        [
            f"Game {game_info['gamePk']}",
            f"{game_info['away']} @ {game_info['home']}",
            game_info.get("date", ""),
            game_info.get("status", ""),
        ],
        [],
        *score_rows,
        [],
        *away_rows,
        [],
        [],
        *home_rows,
    ]

    sheet = service.spreadsheets()

    sheet.values().clear(
        spreadsheetId=SPREADSHEET_ID,
        range="Sheet1",
    ).execute()

    sheet.values().update(
        spreadsheetId=SPREADSHEET_ID,
        range="Sheet1!A1",
        valueInputOption="RAW",
        body={"values": all_rows},
    ).execute()

    print(f"\nWrote {len(all_rows)} rows to Google Sheet.")


# ── CLI helpers ────────────────────────────────────────────────────────────────

def pick_game(sport_id):
    """List today's games for the given level and prompt the user to pick one.

    Args:
        sport_id: MLB Stats API sport ID.

    Returns:
        Tuple of (gamePk, game_info dict).
    """
    today = date.today().isoformat()
    level_name = next(k for k, v in LEVELS.items() if v == sport_id)
    print(f"Fetching {level_name} games for {today}...")
    games = fetch_games(today, sport_id)

    if not games:
        print("No games found for today.")
        sys.exit(1)

    print(f"\n  {'#':<4}  {'Game PK':<10}  {'Away':<31}  {'Home':<31}  Status")
    print(f"  {'-' * 97}")
    for i, g in enumerate(games, 1):
        print(f"  {i:<4}  {g['gamePk']:<10}  {g['away']:<31}  {g['home']:<31}  {g['status']}")

    print()
    choice = input("Enter # or game PK: ").strip()
    try:
        n = int(choice)
        if 1 <= n <= len(games):
            return games[n - 1]["gamePk"], games[n - 1]
        return n, None
    except (ValueError, IndexError):
        print("Invalid selection.")
        sys.exit(1)


def parse_args():
    """Parse command-line arguments for game PK and MiLB level.

    Accepts an optional integer game PK and an optional level string
    (AAA, AA, A+, A, R) in any order.

    Returns:
        Tuple of (game_pk or None, sport_id).
    """
    game_pk = None
    sport_id = LEVELS["AAA"]  # default to Triple-A

    for arg in sys.argv[1:]:
        upper = arg.upper()
        if upper in LEVELS:
            sport_id = LEVELS[upper]
        else:
            try:
                game_pk = int(arg)
            except ValueError:
                print(f"Unknown argument: {arg}")
                print("Usage: python main.py [game_pk] [AAA|AA|A+|A|R]")
                sys.exit(1)

    return game_pk, sport_id


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    """Parse args, fetch game data, print rosters to console, write to sheet."""
    season = date.today().year
    game_pk, sport_id = parse_args()

    game_info = None
    if game_pk is None:
        game_pk, game_info = pick_game(sport_id)

    if game_info is None:
        game_info = fetch_game_summary(game_pk)

    print(f"\nFetching boxscore for game {game_pk}...")
    boxscore = fetch_boxscore(game_pk)

    if not _boxscore_has_stats(boxscore):
        backfill_stats(boxscore, season)

    score = build_score_rows(boxscore)
    away_r, away_h, away_e = score[2][2], score[2][3], score[2][4]
    home_r, home_h, home_e = score[3][2], score[3][3], score[3][4]
    away_name = game_info["away"]
    home_name = game_info["home"]
    print(f"\n  {'SCORE':}")
    print(f"  {'':<4}  {'Team':<31}  R   H   E")
    print(f"  {'-' * 50}")
    print(f"  Away  {away_name:<31}  {away_r:<3} {away_h:<3} {away_e}")
    print(f"  Home  {home_name:<31}  {home_r:<3} {home_h:<3} {home_e}")

    for side in ("away", "home"):
        display_roster(side, boxscore)

    print("\nConnecting to Google Sheets...")
    try:
        service = get_sheets_service()
        write_to_sheet(service, game_info, boxscore)
    except HttpError as err:
        print(f"Google Sheets error: {err}")
        sys.exit(1)

    print()


if __name__ == "__main__":
    main()
