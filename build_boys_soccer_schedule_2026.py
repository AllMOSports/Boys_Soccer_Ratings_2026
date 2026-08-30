#!/usr/bin/env python3
"""
build_boys_soccer_schedule_2026.py

Converts boys_soccer_games_2026_all.json (flat list of games, one row
per game with team1/team2/score1/score2) into
boys_soccer_schedule_2026.json (per-team keyed format:
{"teams": {schoolName: [game, ...]}}), same shape the Sport Detail
snippet's SCHEDULE_URLS/HISTORICAL_SCHEDULE_URLS already expect for every
other sport/season -- this is a direct adaptation of
build_football_schedule_2026.py; the conversion logic itself isn't
sport-specific (it never reads anything about football), only the
docstring/filenames changed. Unlike the fall softball/girls volleyball
versions, the "forfeit"/"overtime" field pair is UNCHANGED here -- boys
soccer has a real overtime concept the same way football does, so
nothing needed renaming or dropping.

Why this exists: boys_soccer_games_2026_all.json comes from the same
kind of pipeline as football's (AllMOSports's per-sport 2026 games
repos), and its schema is flat (team1/team2/score1/score2) rather than
pre-split per team with predicted scores. This script bridges that gap
so the existing front-end code needs zero changes.

No ratings-based fields (predicted_team_score, predicted_opp_score,
off_delta, def_delta, ovr_delta) are computed here -- they're left null
on every game, same null-degradation pattern the front-end already
handles for girls_volleyball, since a boys soccer ratings feed for 2026
presumably won't exist until some point into the season. Once one
exists, a second pass can backfill those fields using the same
league_average + off_rating - def_rating formula used in prior seasons
-- this script does not need to change for that, only a new step added
after it.

home_away is also left null on every game: the source file carries no
home/away indicator, so this can't be determined from what's available.
The front-end already tolerates a null/missing home_away (falls back to
"at" instead of alternating "vs"/"at"); revisit if home/away ever gets
added to the source data.

Usage:
    python build_boys_soccer_schedule_2026.py <input_path_or_url> <output_path>

Example:
    python build_boys_soccer_schedule_2026.py boys_soccer_games_2026_all.json boys_soccer_schedule_2026.json
"""

import json
import sys
import urllib.request
from datetime import datetime, timezone


def load_games(source):
    """Load the flat game list from a local path or an http(s) URL."""
    if source.startswith("http://") or source.startswith("https://"):
        with urllib.request.urlopen(source) as resp:
            return json.load(resp)
    with open(source, "r", encoding="utf-8") as f:
        return json.load(f)


def compute_result(team_score, opp_score):
    """Mirror the W/L/T logic every other season's schedule file uses.
    Returns None (upcoming/unplayed game) if either score is missing."""
    if team_score is None or opp_score is None:
        return None
    if team_score > opp_score:
        return "W"
    if team_score < opp_score:
        return "L"
    return "T"


def make_game_entry(date, opponent, team_score, opp_score, forfeit, overtime):
    return {
        "date": date,
        "opponent": opponent,
        "home_away": None,  # not present in source; see module docstring
        "team_score": team_score,
        "opp_score": opp_score,
        "result": compute_result(team_score, opp_score),
        # Ratings-dependent fields -- intentionally null until a boys
        # soccer ratings feed exists (see module docstring).
        "predicted_team_score": None,
        "predicted_opp_score": None,
        "off_delta": None,
        "def_delta": None,
        "ovr_delta": None,
        # Carried through from the source file for future use (not yet
        # read by the front-end, but cheap to keep rather than discard).
        "forfeit": bool(forfeit),
        "overtime": bool(overtime),
    }


def build_schedule(games, season=2026):
    teams = {}

    for g in games:
        date = g.get("date")
        team1 = g.get("team1")
        team2 = g.get("team2")
        score1 = g.get("score1")
        score2 = g.get("score2")
        forfeit = g.get("forfeit", False)
        overtime = g.get("overtime", False)

        if not team1 or not team2:
            # Malformed row -- skip rather than crash the whole build.
            print(f"Skipping malformed game (missing team name): {g}", file=sys.stderr)
            continue

        teams.setdefault(team1, []).append(
            make_game_entry(date, team2, score1, score2, forfeit, overtime)
        )
        teams.setdefault(team2, []).append(
            make_game_entry(date, team1, score2, score1, forfeit, overtime)
        )

    # Keep each team's games in chronological order (ISO date strings
    # sort correctly as plain strings; None dates sort last).
    for schedule in teams.values():
        schedule.sort(key=lambda entry: entry["date"] or "9999-99-99")

    return {
        "season": season,
        "generated": datetime.now(timezone.utc).isoformat(),
        "teams": teams,
    }


def main():
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(1)

    input_source, output_path = sys.argv[1], sys.argv[2]

    games = load_games(input_source)
    schedule = build_schedule(games)

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(schedule, f, indent=2)

    team_count = len(schedule["teams"])
    game_count = len(games)
    print(f"Built {output_path}: {team_count} teams, {game_count} source games.")


if __name__ == "__main__":
    main()
