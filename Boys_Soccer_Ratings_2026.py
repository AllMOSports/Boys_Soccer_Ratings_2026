import requests
from bs4 import BeautifulSoup
import json
import csv
import re
import pandas as pd
from datetime import datetime, date, timedelta
import time
import urllib.request
from datetime import timezone
from collections import Counter
 
# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------
 
SEASON_YEAR   = 2026
SEASON_START  = date(2026, 8, 1)
SEASON_END    = date(2026, 12, 15)   # kept same as football -- boys soccer can run into December
BASE_URL      = "https://www.mshsaa.org/activities/scoreboard.aspx?alg=33&date={}"
MAX_POINTS    = 20
OUTPUT_PATH   = f"boys_soccer_ratings_{SEASON_YEAR}.json"
CSV_PATH      = f"boys_soccer_scoreboard_{SEASON_YEAR}.csv"
CLASSIFICATIONS_PATH  = "classifications.json"
 
# --- Schedule build (formerly build_boys_soccer_schedule_2026.py) ---
# Input is the flat games file from scrape_boys_soccer_games_2026.py
# (every game, played or upcoming, including non-classified opponents).
# Can be a local path or an http(s) URL.
GAMES_ALL_SOURCE      = f"boys_soccer_games_{SEASON_YEAR}_all.json"
SCHEDULE_JSON_PATH    = f"boys_soccer_schedule_{SEASON_YEAR}.json"
SCHEDULE_CSV_PATH     = f"boys_soccer_schedule_{SEASON_YEAR}.csv"
SCHOOLS_CSV           = "mshsaa_schools.csv"
ITERATIONS            = 1000
LEARNING_RATE         = 0.1
 
# --- v2 rating engine settings (soft weighting + shrinkage, replaces the
#     old hard Phase-2 cutoff) ---
# Retuned from the football values (threshold=40, K=3.0, cap=28) to fit
# soccer's compressed scoring scale. Football off/def ratings are fit in
# ~points-per-game units (league avg ~20-30); soccer will fit in
# goals-per-game units (league avg usually ~1.5-2.5), roughly a 10-12x
# smaller scale. Scaling threshold and cap down accordingly, and adding a
# bit more shrinkage since single-goal margins carry more randomness
# (bounces, one set piece) relative to true team strength than football's
# larger point margins do.
COMPETITIVE_THRESHOLD = 3     # half-weight at a 3-goal combined rating gap
REGULARIZATION_K      = 4.0   # slightly more shrinkage than football's 3.0
MOV_CAP               = 6     # a single goal-margin can contribute at most 6 "goals" of error
 
# ---------------------------------------------------------------------------
# MANUAL GAMES (not listed on MSHSAA Scoreboard)
# ---------------------------------------------------------------------------
# Add any games missing from the MSHSAA scoreboard here.
# Format: ("YYYY-MM-DD", "Team 1 Name", score1, "Team 2 Name", score2)
# Team names must match exactly the names in classifications.json.
# Clean slate for Boys Soccer -- populate as you identify missing games
# against the MSHSAA scoreboard, same as the football workflow.
 
MANUAL_GAMES = [
]
 
# ---------------------------------------------------------------------------
# SCORE CORRECTIONS (from Suspicious_Scores_-_Boys_Soccer.xlsx review)
# ---------------------------------------------------------------------------
# Fixes for games that scraped with a bad score. Matched by date + the two
# team names (order-independent), then each team's score is set explicitly
# -- so this works regardless of which team the scraper put in the home
# slot vs. the away slot.
# Format: ("YYYY-MM-DD", "Team A", correct_score_A, "Team B", correct_score_B)
 
SCORE_CORRECTIONS = [
]
 
# ---------------------------------------------------------------------------
# EXCLUDED GAMES (from Suspicious_Scores_-_Boys_Soccer.xlsx review)
# ---------------------------------------------------------------------------
# Games to drop entirely -- confirmed bad/unverifiable entries rather than
# fixable score typos. Matched by date + the two team names (order-independent).
# Format: ("YYYY-MM-DD", "Team A", "Team B")
 
EXCLUDED_GAMES = [
]
 
# ---------------------------------------------------------------------------
# EXCLUDED TEAMS
# ---------------------------------------------------------------------------
# Teams removed from the ratings entirely. Every game involving them is
# dropped before the fit (same treatment as a non-MSHSAA opponent), so they
# don't appear in any JSON/CSV output and don't affect opponents' ratings.
# Names must match classifications.json exactly.
 
EXCLUDED_TEAMS = [
]
 
# Scoreboard names that couldn't be resolved to a classifications.json name,
# collected during scraping so they can be reported instead of silently lost.
UNRESOLVED_NAMES = Counter()
 
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/152.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Referer": "https://www.mshsaa.org/"
}
 
# ---------------------------------------------------------------------------
# HTTP SESSION (connection reuse + retry on transient failures)
# ---------------------------------------------------------------------------
# Days that timeout right at the 20s ceiling get one retry with a short
# backoff before we give up on them. A shared Session reuses the underlying
# TCP connection instead of opening a fresh one per request, which by
# itself often reduces the frequency of these near-ceiling timeouts.
 
def build_session():
    from requests.adapters import HTTPAdapter
    try:
        from urllib3.util.retry import Retry
    except ImportError:
        from requests.packages.urllib3.util.retry import Retry
 
    session = requests.Session()
    retry = Retry(
        total=1,                      # one retry after the first failure
        connect=1,
        read=1,
        backoff_factor=1.5,           # short pause before the retry
        status_forcelist=[500, 502, 503, 504],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=1, pool_maxsize=1)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session
 
# ---------------------------------------------------------------------------
# CLASSIFICATIONS
# ---------------------------------------------------------------------------
 
def load_classifications(path=CLASSIFICATIONS_PATH):
    """Return team_to_class and team_to_district dicts keyed by school name."""
    with open(path) as f:
        data = json.load(f)
    team_to_class    = {}
    team_to_district = {}
    for entry in data["teams"]:
        school = entry["school"]
        team_to_class[school]    = entry["classification"]
        team_to_district[school] = entry["district"]
    return team_to_class, team_to_district
 
 
# ---------------------------------------------------------------------------
# NAME RESOLUTION
# ---------------------------------------------------------------------------
 
def build_id_to_classname(team_to_class, schools_csv=SCHOOLS_CSV):
    """
    Build { school_id_str : classification_name } by exact-matching
    mshsaa_schools.csv names to classifications.json names after stripping
    the ' High School' suffix. No fuzzy matching used.
 
    MANUAL_OVERRIDES covers schools whose mshsaa_schools.csv name does not
    match their classifications.json name (renamed/merged co-op schools,
    etc). Empty for now -- populate by looking up each school's ID directly
    from the MSHSAA boys soccer scoreboard pages as you find mismatches.
    """
    MANUAL_OVERRIDES = {
"194": "Smith-Cotton",
"549": "St. Mary's South Side",
"465": "Stover",
"207": "Sullivan",
"198": "Truman",
    }
 
    df = pd.read_csv(schools_csv)
    known_class_names = set(team_to_class.keys())
 
    id_to_classname = {}
    for _, row in df.iterrows():
        full_name = row["school_name"]
        sid       = str(row["school_id"])
        stripped  = full_name.replace(" High School", "").strip()
 
        if stripped in known_class_names:
            id_to_classname[sid] = stripped
        elif full_name in known_class_names:
            id_to_classname[sid] = full_name
 
    # Apply manual overrides last so they always take priority -- but only
    # ones that point at a name in THIS season's classifications.json. A
    # stale override (co-op renamed/dissolved since it was written) would
    # otherwise put games under a team name with no class or district.
    stale = {sid: n for sid, n in MANUAL_OVERRIDES.items()
             if n not in known_class_names}
    for sid, n in MANUAL_OVERRIDES.items():
        if sid not in stale:
            id_to_classname[sid] = n
    if stale:
        print(f"  [name-resolve] Skipped {len(stale)} MANUAL_OVERRIDES entries "
              f"whose name isn't in {CLASSIFICATIONS_PATH} (update or remove):")
        for sid, n in sorted(stale.items(), key=lambda x: x[1]):
            print(f"    s={sid}: {n}")
 
    print(f"  [name-resolve] {len(id_to_classname)} schools mapped by ID "
          f"({len(MANUAL_OVERRIDES) - len(stale)} via manual overrides)")
    return id_to_classname
 
 
def resolve_name(cell, id_to_classname, known_teams):
    """
    Resolve a scoreboard table cell to a classification name.
 
    Step 1: Extract s= ID from href → look up in id_to_classname.
            Handles renamed/merged schools (e.g. 'Scott City with Chaffee'
            → 'Scott City') because the ID in the href never changes.
    Step 2: Exact match of display text against known_teams.
            Handles co-op names that exist in classifications as-is.
    Returns None if unresolvable — game will be skipped.
    """
    a = cell.find("a", href=lambda h: h and "/MySchool/Schedule.aspx" in h)
    if not a:
        return None
 
    # Step 1: ID-based lookup
    href  = a.get("href", "")
    match = re.search(r"[?&]s=(\d+)", href, re.IGNORECASE)
    if match:
        sid = match.group(1)
        if sid in id_to_classname and id_to_classname[sid] in known_teams:
            return id_to_classname[sid]
    else:
        sid = "?"
 
    # Step 2: Exact display text match
    display_text = a.get_text(strip=True)
    if display_text in known_teams:
        return display_text
 
    UNRESOLVED_NAMES[f"{display_text} (s={sid})"] += 1
    return None
 
 
# ---------------------------------------------------------------------------
# SCRAPING
# ---------------------------------------------------------------------------
 
def is_mshsaa_team(cell):
    return cell.find(
        "a", href=lambda h: h and "/MySchool/Schedule.aspx" in h
    ) is not None
 
 
def parse_score(text):
    text = text.strip()
    if not text:
        return None
    try:
        score = int(text)
    except ValueError:
        return None
    return score if 0 <= score <= MAX_POINTS else None
 
 
def is_forfeit(c1, c2):
    return "forfeit" in (c1.get_text() + c2.get_text()).lower()
 
 
def scrape_date(target_date, id_to_classname, known_teams, session):
    url = BASE_URL.format(target_date.strftime("%m%d%Y"))
    try:
        # (connect_timeout, read_timeout) -- 10s to connect, 25s to read.
        # 25s (vs. the old flat 20s) gives borderline-slow responses (the
        # ~20.6-20.9s ones you saw) a real chance to finish instead of
        # being cut off right before they would have succeeded.
        resp = session.get(url, timeout=(10, 25), headers=HEADERS)
        resp.raise_for_status()
    except requests.exceptions.Timeout as e:
        print(f"  TIMEOUT {target_date}: {e}")
        return [], "timeout"
    except requests.RequestException as e:
        print(f"  Failed {target_date}: {e}")
        return [], "error"
 
    soup  = BeautifulSoup(resp.text, "html.parser")
    games = []
 
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 3:
            continue
        if "final" not in rows[-1].get_text().lower():
            continue
 
        t1c = rows[1].find_all("td")
        t2c = rows[2].find_all("td")
        if len(t1c) < 3 or len(t2c) < 3:
            continue
        if not is_mshsaa_team(t1c[1]) or not is_mshsaa_team(t2c[1]):
            continue
        if is_forfeit(t1c[1], t2c[1]):
            continue
 
        name1 = resolve_name(t1c[1], id_to_classname, known_teams)
        name2 = resolve_name(t2c[1], id_to_classname, known_teams)
 
        if name1 is None or name2 is None:
            continue
 
        s1 = parse_score(t1c[2].get_text())
        s2 = parse_score(t2c[2].get_text())
        if s1 is None or s2 is None:
            continue
 
        games.append((
            target_date.strftime("%Y-%m-%d"),
            name1, s1,
            name2, s2
        ))
 
    return games, None
 
 
def scrape_full_season(id_to_classname, known_teams):
    all_games     = []
    current       = SEASON_START
    scrape_t0     = time.perf_counter()
    slow_days     = []   # (date, seconds) for anything taking > 3s
    failed_days   = []   # (date, reason) for anything that never succeeded
    session       = build_session()
 
    while current <= min(SEASON_END, date.today()):
        day_t0 = time.perf_counter()
        print(f"  Scraping {current}...", end=" ", flush=True)
        day_games, fail_reason = scrape_date(current, id_to_classname, known_teams, session)
        all_games.extend(day_games)
        day_elapsed = time.perf_counter() - day_t0
        print(f"{len(day_games)} games ({day_elapsed:.1f}s)")
        if day_elapsed > 3.0:
            slow_days.append((current, day_elapsed))
        if fail_reason is not None:
            failed_days.append((current, fail_reason))
        current += timedelta(days=1)
        time.sleep(0.5)
 
    scrape_elapsed = time.perf_counter() - scrape_t0
    print(f"\n  [TIMING] Scraping took {scrape_elapsed:.1f}s total "
          f"for {len(all_games)} games.")
    if slow_days:
        print(f"  [TIMING] {len(slow_days)} slow day(s) (>3s each):")
        for d, secs in slow_days:
            print(f"    {d}: {secs:.1f}s")
    if failed_days:
        print(f"\n  *** {len(failed_days)} date(s) NEVER returned data, "
              f"even after retry -- these dates may be missing real "
              f"games. Check them manually against MSHSAA and add via "
              f"MANUAL_GAMES if needed: ***")
        for d, reason in failed_days:
            print(f"    {d} ({reason})")
    else:
        print("  All dates returned successfully -- no known data gaps "
              "from scraping failures.")
    if UNRESOLVED_NAMES:
        print(f"\n  *** {len(UNRESOLVED_NAMES)} scoreboard name(s) could not be "
              f"matched to {CLASSIFICATIONS_PATH} -- their games were "
              f"skipped. Add the ID to MANUAL_OVERRIDES if they should count: ***")
        for name, n in UNRESOLVED_NAMES.most_common():
            print(f"    {name}: {n} appearance(s)")
    return all_games
 
 
def apply_score_corrections(all_games, corrections=SCORE_CORRECTIONS):
    """
    Fix known-bad scores in place. Matches each game by date + the two team
    names (order-independent), then overwrites each named team's score with
    the corrected value -- regardless of which position (t1/t2) that team
    ended up in during scraping.
    """
    lookup = {}
    for date_str, team_a, score_a, team_b, score_b in corrections:
        lookup[(date_str, frozenset([team_a, team_b]))] = {team_a: score_a, team_b: score_b}
 
    corrected = 0
    fixed_games = []
    for date_str, t1, s1, t2, s2 in all_games:
        key = (date_str, frozenset([t1, t2]))
        fix = lookup.get(key)
        if fix is not None:
            new_s1 = fix.get(t1, s1)
            new_s2 = fix.get(t2, s2)
            if (new_s1, new_s2) != (s1, s2):
                corrected += 1
            fixed_games.append((date_str, t1, new_s1, t2, new_s2))
        else:
            fixed_games.append((date_str, t1, s1, t2, s2))
 
    if corrected:
        print(f"  Corrected {corrected} game score(s) via SCORE_CORRECTIONS.")
    else:
        print("  No SCORE_CORRECTIONS matched (nothing changed).")
 
    return fixed_games
 
 
def apply_exclusions(all_games, exclusions=EXCLUDED_GAMES):
    """
    Drop games confirmed bad/unverifiable. Matches by date + the two team
    names (order-independent).
    """
    exclude_keys = {(date_str, frozenset([team_a, team_b]))
                     for date_str, team_a, team_b in exclusions}
 
    filtered_games = [
        g for g in all_games
        if (g[0], frozenset([g[1], g[3]])) not in exclude_keys
    ]
 
    removed = len(all_games) - len(filtered_games)
    if removed:
        print(f"  Removed {removed} excluded game(s) via EXCLUDED_GAMES.")
    else:
        print("  No EXCLUDED_GAMES matched (nothing removed).")
 
    return filtered_games
 
 
def apply_team_exclusions(all_games, team_to_class, excluded=EXCLUDED_TEAMS):
    """Drop every game involving a team in EXCLUDED_TEAMS."""
    excluded = set(excluded)
    typos = sorted(excluded - set(team_to_class))
    if typos:
        print(f"  WARNING: EXCLUDED_TEAMS name(s) not in {CLASSIFICATIONS_PATH} "
              f"(check spelling): {typos}")
 
    kept = [g for g in all_games if g[1] not in excluded and g[3] not in excluded]
    removed = len(all_games) - len(kept)
    print(f"  Excluded {len(excluded)} team(s); removed {removed} game(s) "
          f"involving them.")
    return kept
 
 
def deduplicate_games(all_games):
    """
    Remove duplicate games where the same two teams played on the same date
    with the same scores, regardless of which team is listed as home or away.
 
    A game is considered a duplicate if another game exists with:
      - The same date
      - The same two team names (in either order)
      - The same two scores (in either order)
 
    The key is built from a frozenset of (team, score) pairs so that
    (Date, Team A, 54, Team B, 13) and (Date, Team B, 13, Team A, 54)
    produce the same key and only one is kept.
    """
    seen         = set()
    unique_games = []
    duplicates   = 0
 
    for game in all_games:
        date_str, t1, s1, t2, s2 = game
        # Key is date + frozenset of team names only — order independent.
        # Scores are intentionally excluded so that (Team A home, Team B away)
        # and (Team B home, Team A away) on the same date are always treated
        # as the same game regardless of which score appears first.
        key = (date_str, frozenset([t1, t2]))
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        unique_games.append(game)
 
    if duplicates:
        print(f"  Removed {duplicates} duplicate game(s). "
              f"{len(unique_games)} unique games remain.")
    else:
        print(f"  No duplicates found. {len(unique_games)} games.")
 
    return unique_games
 
 
def report_missing_teams(all_games, team_to_class):
    """
    After scraping is complete, compare every team in classifications.json
    against the teams that actually appeared in scraped games.
    Print only the teams that have zero games — these are the ones that
    genuinely need attention (either their ID needs adding or their
    classifications.json name needs correcting).
    """
    teams_with_games = set()
    for _, t1, _, t2, _ in all_games:
        teams_with_games.add(t1)
        teams_with_games.add(t2)
 
    missing = sorted(
        t for t in team_to_class
        if t not in teams_with_games and t not in EXCLUDED_TEAMS
    )
 
    if missing:
        print(f"\n  MISSING TEAMS: {len(missing)} classification schools have "
              f"no games in the scraped data.")
        print(f"  These teams need attention — either their MSHSAA page shows")
        print(f"  a different name than classifications.json, or they did not")
        print(f"  play any games this season.")
        print(f"  Missing: {missing}\n")
    else:
        print("\n  All classification schools have at least one game. \n")
 
 
def report_teams_not_rated(ovr_rating, team_to_class):
    """
    Final summary: every classifications.json team that is NOT in the
    ratings output, with the reason. Returns the list of school names.
    """
    not_rated = sorted(t for t in team_to_class if t not in ovr_rating)
 
    print("\n" + "=" * 70)
    if not not_rated:
        print(f"  All {len(team_to_class)} classification teams are included "
              f"in the ratings.")
    else:
        print(f"  TEAMS NOT INCLUDED IN RATINGS: {len(not_rated)} of "
              f"{len(team_to_class)}")
        for t in not_rated:
            reason = ("excluded via EXCLUDED_TEAMS" if t in EXCLUDED_TEAMS
                      else "no completed games in scraped data")
            print(f"    - {t} (Class {team_to_class[t]}): {reason}")
    print("=" * 70)
    return not_rated
 
 
# ---------------------------------------------------------------------------
# CSV OUTPUT
# ---------------------------------------------------------------------------
 
def save_csv(all_games):
    with open(CSV_PATH, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["Date", "Home Team", "Home Score", "Away Team", "Away Score"])
        for date_str, t1, s1, t2, s2 in all_games:
            writer.writerow([date_str, t1, s1, t2, s2])
    print(f"Saved {len(all_games)} games to {CSV_PATH}")
 
 
# ---------------------------------------------------------------------------
# SCHEDULE BUILD (formerly build_boys_soccer_schedule_2026.py)
# ---------------------------------------------------------------------------
# Converts the flat games file (team1/team2/score1/score2, one row per match)
# into the per-team schedule file the Sport Detail snippet reads:
#   {"season": ..., "generated": ..., "teams": {schoolName: [game, ...]}}
# Logic is unchanged from the standalone script, so the JSON output is
# identical apart from the "generated" timestamp.
#
# Ratings-dependent fields (predicted_team_score, predicted_opp_score,
# off_delta, def_delta, ovr_delta) are still written as null, same as the
# standalone script.
# home_away is null because the source file has no home/away indicator;
# the front-end falls back to "at".
# "forfeit" and "overtime" are both carried through from the source file.
 
def load_games_all(source=GAMES_ALL_SOURCE):
    """Load the flat game list from a local path or an http(s) URL."""
    if source.startswith("http://") or source.startswith("https://"):
        with urllib.request.urlopen(source) as resp:
            return json.load(resp)
    with open(source, "r", encoding="utf-8") as f:
        return json.load(f)
 
 
def compute_result(team_score, opp_score):
    """W/L/T, or None for an upcoming/unplayed game (either score missing)."""
    if team_score is None or opp_score is None:
        return None
    if team_score > opp_score:
        return "W"
    if team_score < opp_score:
        return "L"
    return "T"
 
 
def make_schedule_entry(game_date, opponent, team_score, opp_score, forfeit,
                        overtime):
    return {
        "date": game_date,
        "opponent": opponent,
        "home_away": None,
        "team_score": team_score,
        "opp_score": opp_score,
        "result": compute_result(team_score, opp_score),
        "predicted_team_score": None,
        "predicted_opp_score": None,
        "off_delta": None,
        "def_delta": None,
        "ovr_delta": None,
        "forfeit": bool(forfeit),
        "overtime": bool(overtime),
    }
 
 
def build_schedule(games, season=SEASON_YEAR):
    teams = {}
    for g in games:
        game_date = g.get("date")
        team1, team2 = g.get("team1"), g.get("team2")
        score1, score2 = g.get("score1"), g.get("score2")
        forfeit = g.get("forfeit", False)
        overtime = g.get("overtime", False)
 
        if not team1 or not team2:
            print(f"  Skipping malformed game (missing team name): {g}")
            continue
 
        teams.setdefault(team1, []).append(
            make_schedule_entry(game_date, team2, score1, score2, forfeit, overtime))
        teams.setdefault(team2, []).append(
            make_schedule_entry(game_date, team1, score2, score1, forfeit, overtime))
 
    # Chronological per team (ISO strings sort correctly; None dates last)
    for schedule in teams.values():
        schedule.sort(key=lambda entry: entry["date"] or "9999-99-99")
 
    return {
        "season": season,
        "generated": datetime.now(timezone.utc).isoformat(),
        "teams": teams,
    }
 
 
def _write_dict_csv(path, rows, fieldnames):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
 
 
def save_schedule(schedule):
    with open(SCHEDULE_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(schedule, f, indent=2)
 
    fields = ["team", "date", "opponent", "home_away", "team_score",
              "opp_score", "result", "predicted_team_score",
              "predicted_opp_score", "off_delta", "def_delta", "ovr_delta",
              "forfeit", "overtime"]
    rows = [{"team": team, **entry}
            for team in sorted(schedule["teams"])
            for entry in schedule["teams"][team]]
    _write_dict_csv(SCHEDULE_CSV_PATH, rows, fields)
 
    print(f"  Built {SCHEDULE_JSON_PATH} + {SCHEDULE_CSV_PATH}: "
          f"{len(schedule['teams'])} teams, {len(rows)} team-game rows.")
 
 
def run_schedule_build(source=GAMES_ALL_SOURCE):
    """Whole schedule step. A missing/unreadable games file only skips this
    step -- it never stops the ratings from being saved."""
    try:
        games = load_games_all(source)
    except Exception as e:
        print(f"  *** Schedule build SKIPPED -- couldn't load {source}: {e} ***")
        return False
    print(f"  Loaded {len(games)} source games from {source}")
    save_schedule(build_schedule(games))
    return True
 
 
# ---------------------------------------------------------------------------
# RATING ENGINE (v2 -- soft competitiveness weighting + shrinkage regularization)
# ---------------------------------------------------------------------------
#
# Replaces the old two-phase (all games, then hard <=40pt cutoff) approach.
# A dominant team no longer has its rating fully decided by 1-2 close games:
#   1. competitiveness_weight() gives every game a smooth weight based on
#      the current rating gap, instead of an all-or-nothing 40-point cutoff.
#   2. REGULARIZATION_K shrinks updates for teams with little competitive
#      signal, instead of letting a tiny sample fully drive their rating.
#   3. MOV_CAP bounds how much error any single game -- even a fully-weighted
#      one -- can contribute, so no one result can swing a rating too hard.
 
def competitiveness_weight(gap, scale=COMPETITIVE_THRESHOLD):
    """
    Smooth weight in (0, 1] based on the current OVR gap between two teams.
    gap=0            -> weight 1.0 (fully counted)
    gap=scale (40)   -> weight 0.5 (half counted)
    gap=2*scale (80) -> weight 0.2 (mostly discounted, never fully zero)
    """
    return 1.0 / (1.0 + (gap / scale) ** 2)
 
 
def run_iterations(games, teams, off_rating, def_rating, league_avg,
                   iterations, phase_label="Fit"):
    for iteration in range(iterations):
        off_error  = {t: 0.0 for t in teams}
        def_error  = {t: 0.0 for t in teams}
        weight_sum = {t: 0.0 for t in teams}
 
        for t1, t2, actual_s1, actual_s2 in games:
            gap = abs((off_rating[t1] + def_rating[t1]) -
                      (off_rating[t2] + def_rating[t2]))
            w = competitiveness_weight(gap)
 
            predicted_s1 = off_rating[t1] - def_rating[t2] + league_avg
            predicted_s2 = off_rating[t2] - def_rating[t1] + league_avg
 
            error_s1 = actual_s1 - predicted_s1
            error_s2 = actual_s2 - predicted_s2
 
            # MOV cap: bound the raw error before it's weighted/accumulated
            error_s1 = max(-MOV_CAP, min(MOV_CAP, error_s1))
            error_s2 = max(-MOV_CAP, min(MOV_CAP, error_s2))
 
            off_error[t1] += w * error_s1
            off_error[t2] += w * error_s2
            def_error[t1] += -w * error_s2
            def_error[t2] += -w * error_s1
 
            weight_sum[t1] += w
            weight_sum[t2] += w
 
        for team in teams:
            # Shrinkage: denominator is (weighted games) + K, not just raw
            # games played. Teams with low competitive weight get smaller,
            # more conservative updates instead of being fully driven by
            # 1-2 games.
            denom = weight_sum[team] + REGULARIZATION_K
            off_rating[team] += (off_error[team] / denom) * LEARNING_RATE
            def_rating[team] += (def_error[team] / denom) * LEARNING_RATE
 
        if (iteration + 1) % 100 == 0:
            print(f"  [{phase_label}] Iteration {iteration + 1}/{iterations} complete")
 
 
def calculate_ratings(all_games, iterations=ITERATIONS):
    games = [(t1, t2, s1, s2) for _, t1, s1, t2, s2 in all_games]
 
    teams = list({t for t1, t2, _, _ in games for t in (t1, t2)})
    if not teams:
        return {}, {}, {}, 0
 
    all_scores = [s for _, _, s1, s2 in games for s in (s1, s2)]
    league_avg = sum(all_scores) / len(all_scores)
    print(f"  League average: {league_avg:.2f} points per game")
 
    off_rating = {t: 0.0 for t in teams}
    def_rating = {t: 0.0 for t in teams}
 
    print(f"\n  Running rating fit ({iterations} iterations, soft-weighted "
          f"by competitiveness [scale={COMPETITIVE_THRESHOLD}], "
          f"shrinkage K={REGULARIZATION_K}, MOV cap={MOV_CAP})...")
    print(f"  [TIMING] {len(teams)} teams, {len(games)} games going into the fit.")
    engine_t0 = time.perf_counter()
    run_iterations(games, teams, off_rating, def_rating, league_avg,
                   iterations=iterations, phase_label="Fit")
    print(f"  [TIMING] Rating fit took {time.perf_counter() - engine_t0:.1f}s.")
 
    ovr_rating = {t: round(off_rating[t] + def_rating[t], 2) for t in teams}
    return off_rating, def_rating, ovr_rating, league_avg
# ---------------------------------------------------------------------------
# JSON OUTPUT
# ---------------------------------------------------------------------------
 
def build_team_entries(off_rating, def_rating, ovr_rating,
                       team_to_class, team_to_district,
                       class_filter=None):
    all_teams = list(ovr_rating.keys())
 
    pool = (
        [t for t in all_teams if team_to_class.get(t) == class_filter]
        if class_filter is not None
        else all_teams
    )
 
    ovr_sorted = sorted(pool, key=lambda t: ovr_rating[t], reverse=True)
    off_sorted = sorted(pool, key=lambda t: off_rating[t], reverse=True)
    def_sorted = sorted(pool, key=lambda t: def_rating[t], reverse=True)
 
    ovr_rank = {t: i + 1 for i, t in enumerate(ovr_sorted)}
    off_rank = {t: i + 1 for i, t in enumerate(off_sorted)}
    def_rank = {t: i + 1 for i, t in enumerate(def_sorted)}
 
    return [
        {
            "ovr_rank":       ovr_rank[t],
            "school":         t,
            "classification": team_to_class.get(t),
            "district":       team_to_district.get(t),
            "ovr_rating":     ovr_rating[t],
            "off_rating":     round(off_rating[t], 2),
            "off_rank":       off_rank[t],
            "def_rating":     round(def_rating[t], 2),
            "def_rank":       def_rank[t],
        }
        for t in ovr_sorted
    ]
 
 
def save_overall_json(off_rating, def_rating, ovr_rating, league_avg,
                      team_to_class, team_to_district):
    entries = build_team_entries(off_rating, def_rating, ovr_rating,
                                 team_to_class, team_to_district)
    output = {
        "last_updated":   datetime.now().strftime("%B %d, %Y at %I:%M %p"),
        "league_average": round(league_avg, 2),
        "teams": entries,
    }
    with open(OUTPUT_PATH, "w") as f:
        json.dump(output, f, indent=2)
 
    print(f"Saved {len(entries)} teams to {OUTPUT_PATH}")
    print("Top 5 overall:")
    for e in entries[:5]:
        print(f"  {e['ovr_rank']}. {e['school']} (Class {e['classification']}) "
              f"| OVR: {e['ovr_rating']:+.2f} "
              f"| OFF: {e['off_rating']:+.2f} "
              f"| DEF: {e['def_rating']:+.2f}")
 
 
def save_class_jsons(off_rating, def_rating, ovr_rating, league_avg,
                     team_to_class, team_to_district):
    for cls in sorted(set(team_to_class.values())):  # 2026: Classes 1-4
        entries = build_team_entries(off_rating, def_rating, ovr_rating,
                                     team_to_class, team_to_district,
                                     class_filter=cls)
        if not entries:
            print(f"  Class {cls}: no teams found — skipping.")
            continue
 
        path = f"boys_soccer_ratings_{SEASON_YEAR}_class{cls}.json"
        output = {
            "last_updated":   datetime.now().strftime("%B %d, %Y at %I:%M %p"),
            "league_average": round(league_avg, 2),
            "classification": cls,
            "teams": entries,
        }
        with open(path, "w") as f:
            json.dump(output, f, indent=2)
 
        print(f"  Class {cls}: {len(entries)} teams → {path}")
        print("    Top 3: " + " | ".join(
            f"{e['ovr_rank']}. {e['school']} ({e['ovr_rating']:+.2f})"
            for e in entries[:3]
        ))
 
 
 
# ---------------------------------------------------------------------------
# CSV RANKINGS OUTPUT
# ---------------------------------------------------------------------------
 
def save_rankings_csv(off_rating, def_rating, ovr_rating,
                      team_to_class, team_to_district,
                      class_filter=None):
    """
    Save a rankings CSV for either all teams (class_filter=None) or a
    specific class.  Rankings (OFF Rank, DEF Rank, OVR Rank) are computed
    within the pool so class CSVs show class-specific ranks.
 
    Columns: School, OFF Rating, DEF Rating, OVR Rating,
             OFF Rank, DEF Rank, OVR Rank
    """
    all_teams = list(ovr_rating.keys())
 
    pool = (
        [t for t in all_teams if team_to_class.get(t) == class_filter]
        if class_filter is not None
        else all_teams
    )
 
    if not pool:
        label = f"Class {class_filter}" if class_filter else "Overall"
        print(f"  {label}: no teams — skipping CSV.")
        return
 
    ovr_sorted = sorted(pool, key=lambda t: ovr_rating[t], reverse=True)
    off_sorted = sorted(pool, key=lambda t: off_rating[t], reverse=True)
    def_sorted = sorted(pool, key=lambda t: def_rating[t], reverse=True)
 
    ovr_rank = {t: i + 1 for i, t in enumerate(ovr_sorted)}
    off_rank = {t: i + 1 for i, t in enumerate(off_sorted)}
    def_rank = {t: i + 1 for i, t in enumerate(def_sorted)}
 
    rows = [
        {
            "School":      t,
            "OFF Rating":  round(off_rating[t], 2),
            "DEF Rating":  round(def_rating[t], 2),
            "OVR Rating":  round(ovr_rating[t], 2),
            "OFF Rank":    off_rank[t],
            "DEF Rank":    def_rank[t],
            "OVR Rank":    ovr_rank[t],
        }
        for t in ovr_sorted
    ]
 
    df = pd.DataFrame(rows, columns=[
        "School", "OFF Rating", "DEF Rating", "OVR Rating",
        "OFF Rank", "DEF Rank", "OVR Rank"
    ])
 
    if class_filter is None:
        path  = f"boys_soccer_rankings_{SEASON_YEAR}_all.csv"
        label = "All teams"
    else:
        path  = f"boys_soccer_rankings_{SEASON_YEAR}_class{class_filter}.csv"
        label = f"Class {class_filter}"
 
    df.to_csv(path, index=False)
    print(f"  {label}: {len(df)} teams — {path}")
 
 
def save_all_rankings_csvs(off_rating, def_rating, ovr_rating,
                           team_to_class, team_to_district):
    """Save overall + one CSV per class."""
    save_rankings_csv(off_rating, def_rating, ovr_rating,
                      team_to_class, team_to_district,
                      class_filter=None)
    for cls in sorted(set(team_to_class.values())):  # 2026: Classes 1-4
        save_rankings_csv(off_rating, def_rating, ovr_rating,
                          team_to_class, team_to_district,
                          class_filter=cls)
 
# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
 
if __name__ == "__main__":
    print(f"=== MSHSAA Boys Soccer Ratings {SEASON_YEAR} ===")
 
    print("\nLoading classifications...")
    team_to_class, team_to_district = load_classifications()
    known_teams = set(team_to_class.keys())
    print(f"  Loaded {len(team_to_class)} teams from {CLASSIFICATIONS_PATH}")
 
    print("\nBuilding school ID → classification name lookup...")
    id_to_classname = build_id_to_classname(team_to_class, SCHOOLS_CSV)
 
    print("\nScraping season scoreboard...")
    all_games = scrape_full_season(id_to_classname, known_teams)
    print(f"\nTotal valid games (before deduplication): {len(all_games)}")
    if not all_games:
        print("No games found — exiting.")
        exit(1)
 
    if MANUAL_GAMES:
        print(f"\nAdding {len(MANUAL_GAMES)} manual game(s)...")
        all_games.extend(MANUAL_GAMES)
 
    print("\nApplying score corrections...")
    all_games = apply_score_corrections(all_games)
 
    print("\nApplying game exclusions...")
    all_games = apply_exclusions(all_games)
 
    print("\nDeduplicating games...")
    all_games = deduplicate_games(all_games)
 
    print("\nChecking for missing teams...")
    report_missing_teams(all_games, team_to_class)
 
    print("\nApplying team exclusions...")
    all_games = apply_team_exclusions(all_games, team_to_class)
 
    print("Saving scoreboard CSV...")
    save_csv(all_games)
 
    print(f"\nRunning ratings engine ({ITERATIONS} iterations)...")
    off_rating, def_rating, ovr_rating, league_avg = calculate_ratings(all_games)
 
    print("\nSaving overall ratings JSON...")
    save_overall_json(off_rating, def_rating, ovr_rating, league_avg,
                      team_to_class, team_to_district)
 
    print("\nSaving per-class ratings JSONs...")
    save_class_jsons(off_rating, def_rating, ovr_rating, league_avg,
                     team_to_class, team_to_district)
 
    print("\nSaving rankings CSVs...")
    save_all_rankings_csvs(off_rating, def_rating, ovr_rating,
                           team_to_class, team_to_district)
 
    print("\nBuilding schedule files...")
    run_schedule_build()
 
    report_teams_not_rated(ovr_rating, team_to_class)
 
    print("\n=== Done ===")
