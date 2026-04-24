"""
MiLB Game Time Stats
Displays both teams' rosters and current season stats for a minor league game
using the public MLB Stats API (statsapi.mlb.com).

Usage:
    python main.py                   # lists today's AAA games, prompts for selection
    python main.py AA                # lists today's AA games instead
    python main.py 816595            # jump straight to a specific game by PK
    python main.py 816595 AA         # game PK at a specific level
"""

import requests
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

BASE_URL = "https://statsapi.mlb.com/api/v1"

# MLB Stats API sport IDs for each MiLB level
LEVELS = {"AAA": 11, "AA": 12, "A+": 13, "A": 14, "R": 16}


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
            })
    return games


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


def fmt_hitting(s):
    """Format a seasonStats batting dict into a readable stat line.

    Args:
        s: Batting stats dict from the boxscore seasonStats field.

    Returns:
        Formatted string, or 'no stats yet' if the player has no games played.
    """
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
    """Format a seasonStats pitching dict into a readable stat line.

    Args:
        s: Pitching stats dict from the boxscore seasonStats field.

    Returns:
        Formatted string, or 'no stats yet' if the pitcher has no innings logged.
    """
    if not s or float(s.get("inningsPitched", 0)) == 0.0:
        return "no stats yet"
    return (
        f"ERA {s.get('era', '---')}  "
        f"{s.get('wins', 0)}-{s.get('losses', 0)}  "
        f"IP {s.get('inningsPitched', '0.0'):>5}  "
        f"K {s.get('strikeOuts', 0):>3}  "
        f"WHIP {s.get('whip', '---')}"
    )


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

    # Lineup: use submitted batting order, else fall back to all non-pitchers
    lineup_ids = batting_order or [
        int(k[2:]) for k, p in players.items()
        if p.get("position", {}).get("type") != "Pitcher"
    ]

    # Pitching: in-game pitchers if available, else the available bullpen
    pitcher_ids = pitchers or bullpen

    # --- Batting order ---
    print(f"\n  {'#':>2}  {'POS':<4}  {'NAME':<27}  SEASON STATS")
    print(f"  {'-' * 68}")
    for i, pid in enumerate(lineup_ids):
        p = players.get(f"ID{pid}", {})
        pos = p.get("position", {}).get("abbreviation", "?")
        name = p.get("person", {}).get("fullName", "Unknown")
        hitting = p.get("seasonStats", {}).get("batting", {})
        order = str(i + 1) if batting_order else ""
        print(f"  {order:>2}  {pos:<4}  {name:<27}  {fmt_hitting(hitting)}")

    # --- Pitching staff ---
    if pitcher_ids:
        print(f"\n  {'ROLE':<5}  {'NAME':<29}  SEASON STATS")
        print(f"  {'-' * 68}")
        for i, pid in enumerate(pitcher_ids):
            p = players.get(f"ID{pid}", {})
            name = p.get("person", {}).get("fullName", "Unknown")
            pitching = p.get("seasonStats", {}).get("pitching", {})
            # Only label SP if we haven't tracked in-game pitchers yet;
            # once pitchers list is non-empty we know who actually started.
            role = "SP" if i == 0 else "RP"
            print(f"  {role:<5}  {name:<29}  {fmt_pitching(pitching)}")

    # --- Bench ---
    if bench:
        print(f"\n  {'':>2}  {'POS':<4}  BENCH")
        print(f"  {'-' * 68}")
        for pid in bench:
            p = players.get(f"ID{pid}", {})
            pos = p.get("position", {}).get("abbreviation", "?")
            name = p.get("person", {}).get("fullName", "Unknown")
            hitting = p.get("seasonStats", {}).get("batting", {})
            print(f"  {'':>2}  {pos:<4}  {name:<27}  {fmt_hitting(hitting)}")


def pick_game(sport_id):
    """List today's games for the given level and prompt the user to pick one.

    Args:
        sport_id: MLB Stats API sport ID.

    Returns:
        gamePk of the selected game.
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
        return games[n - 1]["gamePk"] if 1 <= n <= len(games) else n
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

    args = sys.argv[1:]
    for arg in args:
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


def main():
    """Entry point: parse args, select a game, and display both rosters."""
    season = date.today().year
    game_pk, sport_id = parse_args()

    if game_pk is None:
        game_pk = pick_game(sport_id)

    print(f"\nFetching boxscore for game {game_pk}...")
    boxscore = fetch_boxscore(game_pk)

    if not _boxscore_has_stats(boxscore):
        backfill_stats(boxscore, season)

    for side in ("away", "home"):
        display_roster(side, boxscore)

    print()


if __name__ == "__main__":
    main()
