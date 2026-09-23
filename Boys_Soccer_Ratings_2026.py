"""
MSHSAA Boys Soccer 2026 -- games, schedule, and ratings in one run
=============================================================================
Consolidates what used to be three separate scripts:
  1. scrape_boys_soccer_games_2026.py     -> games JSON/CSV (strict + _all)
  2. build_boys_soccer_schedule_2026.py   -> per-team schedule JSON/CSV
  3. Boys_Soccer_Ratings_2026.py          -> ratings JSON/CSV, rankings CSVs
 
One scrape of the MSHSAA scoreboard covers everything: every date from
SEASON_START through SEASON_END (no stop at today), so upcoming games land
in the schedule files too. The ratings only use completed games from that
same scrape -- both teams classified, both scores posted, marked Final,
not a forfeit.
 
REQUIRES (same directory):
  - classifications.json   (Boys Soccer's own)
  - mshsaa_schools.csv
  - boys_soccer_manual_name_overrides.json   (optional -- schedule name
    corrections/exclusions/score fills; skipped if missing)
"""
 
import requests
from bs4 import BeautifulSoup
import json
import csv
import re
import pandas as pd
from datetime import datetime, date, timedelta
import time
import socket
import urllib3.util.connection as urllib3_cn
from datetime import timezone
from collections import Counter
 
# Force IPv4: mshsaa.org resolves to IPv4 and IPv6, and the IPv6 route has
# failed before ("Network is unreachable", errno 101). Harmless otherwise.
def _force_ipv4_only():
    return socket.AF_INET
 
urllib3_cn.allowed_gai_family = _force_ipv4_only
 
# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------
 
SEASON_YEAR   = 2026
SEASON_START  = date(2026, 8, 15)
SEASON_END    = date(2026, 12, 1)    # scrapes every date through here, including future dates
BASE_URL      = "https://www.mshsaa.org/activities/scoreboard.aspx?alg=33&date={}"
MAX_POINTS    = 25    # scores above this are treated as blank (from the games scraper)
RATINGS_MAX_POINTS = 20    # games with a score above this aren't rated (from the old ratings scrape)
OUTPUT_PATH   = f"boys_soccer_ratings_{SEASON_YEAR}.json"
CSV_PATH      = f"boys_soccer_scoreboard_{SEASON_YEAR}.csv"
CLASSIFICATIONS_PATH  = "classifications.json"
 
# --- Games files (formerly scrape_boys_soccer_games_2026.py) ---
OUTPUT_JSON           = f"boys_soccer_games_{SEASON_YEAR}.json"       # both teams classified
OUTPUT_CSV            = f"boys_soccer_games_{SEASON_YEAR}.csv"
OUTPUT_JSON_ALL       = f"boys_soccer_games_{SEASON_YEAR}_all.json"   # >=1 team classified
OUTPUT_CSV_ALL        = f"boys_soccer_games_{SEASON_YEAR}_all.csv"
MANUAL_OVERRIDES_PATH = "boys_soccer_manual_name_overrides.json"
REQUEST_DELAY         = 0.5   # seconds between scoreboard requests
 
# --- Schedule files (formerly build_boys_soccer_schedule_2026.py) ---
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
 
# Opponents that didn't resolve to a classifications.json name. They're kept
# in the games/schedule files (as unclassified) but never rated. Listed after
# the scrape so a missed Missouri school stands out -- add its ID to
# MANUAL_OVERRIDES if it should be classified.
UNCLASSIFIED_OPPONENTS = Counter()
 
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
 
 
# ---------------------------------------------------------------------------
# SCRAPING (formerly scrape_boys_soccer_games_2026.py)
# ---------------------------------------------------------------------------
# One pass over every date SEASON_START..SEASON_END. Keeps scheduled and
# completed games, and games against non-classified opponents, for the
# games/schedule files. rated_games_from_all() later picks out the subset
# the ratings use.
 
def resolve_name_or_raw(row, school_cell, id_to_classname, known_teams):
    """
    Resolve a team row to (name, classified: bool).
 
    Primary signal: the <tr>'s data-school attribute, which MSHSAA
    populates for EVERY team row -- Missouri member schools AND
    out-of-state/non-member opponents alike. Confirmed against a live
    scoreboard page: an Edwardsville (Ill.) row has data-school='929'
    even though it has no /MySchool/Schedule.aspx link anywhere in it.
    If that ID matches a known Missouri school ID, use
    classifications.json's canonical name and mark classified=True.
 
    Fallback: if data-school is blank/unknown (a non-member opponent,
    or any row missing the attribute for some other reason), fall back
    to the visible name in td.school > span.name and mark
    classified=False. This replaces the old href-only detection, which
    depended on an <a href="/MySchool/Schedule.aspx..."> being present
    inside the cell -- that link is only rendered for MSHSAA member
    schools, so any row for a non-member opponent (e.g. an out-of-state
    team) was previously invisible to the scraper and the ENTIRE game
    got dropped, not just that side.
    """
    sid = (row.get("data-school") or "").strip()
    if sid and sid in id_to_classname:
        return id_to_classname[sid], True
 
    name_span = school_cell.find("span", class_="name")
    raw = name_span.get_text(strip=True) if name_span else None
    # MSHSAA renders a still-TBD opponent slot's name as the literal
    # template text "(, )" (an empty "Name (City, ST)" pattern) rather
    # than leaving it blank -- normalize that to "" (NOT None) so it
    # reads as "no usable name" without being mistaken for an actual
    # (garbled) team name downstream. Important: this must stay a
    # non-None value. scrape_date() drops the row entirely when this
    # returns None (correctly so, for a cell with no name_span at all --
    # that row is genuinely unusable), but "(, )" is a normal, EXPECTED
    # placeholder for a not-yet-determined opponent, and dropping that
    # row silently drops the whole game before it ever reaches the
    # corrections/exclusions step in apply_manual_overrides(). That
    # exact regression happened once already -- see the Edwardsville
    # case this same "row invisible -> game vanishes" pattern caused
    # earlier, and don't reintroduce it here.
    if raw is not None and re.fullmatch(r"\(\s*,\s*\)", raw):
        raw = ""
    if raw and raw in known_teams:
        return raw, True
    if raw:
        UNCLASSIFIED_OPPONENTS[f"{raw} (s={sid or '?'})"] += 1
    return raw, False
 
 
def parse_score(text):
    text = text.strip()
    if not text:
        return None
    try:
        score = int(text)
    except ValueError:
        return None
    return score if 0 <= score <= MAX_POINTS else None
 
 
def is_forfeit(row1, row2):
    return "forfeit" in (row1.get_text() + row2.get_text()).lower()
 
 
def is_overtime(row1, row2):
    """
    First-pass OT detection: looks for "overtime" or a standalone "OT"
    token in the game's row text (e.g. a "Final/OT" status flag some
    scoreboards use). UNVERIFIED against a real MSHSAA OT game -- confirm
    the actual wording once a live OT game shows up and adjust the regex
    if needed.
    """
    text = row1.get_text() + " " + row2.get_text()
    return bool(re.search(r"overtime|\bOT\b", text, re.IGNORECASE))
 
 
def scrape_date(target_date, id_to_classname, known_teams, session):
    """
    Generalized version of boys_soccer_ratings_2025.py (or whichever prior-season script exists for this sport -- adjust if the naming differs)'s scrape_date():
    scans every row in every table for a cell containing an MSHSAA team
    link (rather than assuming team names always sit at a fixed row/column
    index), so it works whether the table has a score column (completed
    games) or not (scheduled games). Pairs up tables with exactly 2
    team-rows as a single game. Score is captured if present, else None.
    """
    url = BASE_URL.format(target_date.strftime("%m%d%Y"))
    try:
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
        if len(rows) < 2:
            continue
 
        team_rows = []  # list of (name, classified, score, row) for team rows
        for row in rows:
            # td.school + span.name is present for EVERY team row, member
            # or not -- unlike the old <a href="/MySchool/Schedule.aspx">
            # check, which only matches MSHSAA member schools and silently
            # skipped rows for out-of-state/non-member opponents.
            school_cell = row.find("td", class_="school")
            if school_cell is None:
                continue
 
            name, classified = resolve_name_or_raw(row, school_cell, id_to_classname, known_teams)
            if name is None:
                # Has a td.school cell but no usable name text either --
                # too broken to use, skip just this row.
                continue
 
            score_cell = row.find("td", class_="score")
            score = parse_score(score_cell.get_text()) if score_cell else None
 
            team_rows.append((name, classified, score, row))
 
        if len(team_rows) != 2:
            continue  # not a clean 2-team game table -- skip
 
        (name1, classified1, s1, row1), (name2, classified2, s2, row2) = team_rows
        if name1 == name2:
            continue
 
        games.append({
            "date": target_date.strftime("%Y-%m-%d"),
            "team1": name1,
            "team1_classified": classified1,
            "score1": s1,
            "team2": name2,
            "team2_classified": classified2,
            "score2": s2,
            "forfeit": is_forfeit(row1, row2),
            "overtime": is_overtime(row1, row2),
            # Internal only (never written to any output file): whether the
            # scoreboard marks this game Final -- the ratings only use Final
            # games, same as the old ratings-only scrape.
            "_final": "final" in rows[-1].get_text().lower(),
        })
 
    return games, None
 
 
def scrape_full_season(id_to_classname, known_teams):
    all_games   = []
    current     = SEASON_START
    scrape_t0   = time.perf_counter()
    slow_days   = []
    failed_days = []
    session     = build_session()
 
    while current <= SEASON_END:
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
        time.sleep(REQUEST_DELAY)
 
    scrape_elapsed = time.perf_counter() - scrape_t0
    print(f"\n  [TIMING] Scraping took {scrape_elapsed:.1f}s total "
          f"for {len(all_games)} games.")
    if slow_days:
        print(f"  [TIMING] {len(slow_days)} slow day(s) (>3s each):")
        for d, secs in slow_days:
            print(f"    {d}: {secs:.1f}s")
    if failed_days:
        print(f"\n  *** {len(failed_days)} date(s) NEVER returned data, "
              f"even after retry: ***")
        for d, reason in failed_days:
            print(f"    {d} ({reason})")
    else:
        print("  All dates returned successfully -- no known data gaps "
              "from scraping failures.")
    if UNCLASSIFIED_OPPONENTS:
        top = UNCLASSIFIED_OPPONENTS.most_common(25)
        print(f"\n  {len(UNCLASSIFIED_OPPONENTS)} opponent name(s) aren't in "
              f"{CLASSIFICATIONS_PATH} (kept in the schedule, not rated). "
              f"Most frequent -- check for any missed Missouri schools:")
        for name, n in top:
            print(f"    {name}: {n}")
    return all_games
 
 
# ---------------------------------------------------------------------------
# GAMES FILE CLEANUP (formerly scrape_boys_soccer_games_2026.py)
# ---------------------------------------------------------------------------
 
def deduplicate_schedule_games(all_games):
    """Same score-independent dedup key as boys_soccer_ratings_2025.py (or whichever prior-season script exists for this sport -- adjust if the naming differs)."""
    seen = set()
    unique_games = []
    duplicates = 0
    for g in all_games:
        key = (g["date"], frozenset([g["team1"], g["team2"]]))
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        unique_games.append(g)
 
    if duplicates:
        print(f"  Removed {duplicates} duplicate game(s). "
              f"{len(unique_games)} unique games remain.")
    else:
        print(f"  No duplicates found. {len(unique_games)} games.")
    return unique_games
 
 
def load_manual_overrides(path=MANUAL_OVERRIDES_PATH):
    """
    Loads the hand-maintained corrections/exclusions file for games that
    come back with one side unclassified (see strict_games_from_all --
    these never make boys_soccer_games_2026.json, but they DO show up in
    the _all files with a blank/garbled name for the non-MSHSAA side).
 
    corrections: keyed by (date, the ALREADY-classified team's name) so a
    fix holds true regardless of whether a score has been filled in yet --
    score is never part of the match key, and the classified side's name
    is never touched. Maps to the corrected name for the OTHER side.
 
    exclusions: exact (date, team1, team2) triples (as originally scraped,
    pre-correction) for specific bad/duplicate games that should be
    dropped outright rather than corrected. Most one-sided-unclassified
    junk doesn't need an entry here at all -- see the both-sides-
    unclassified drop rule in apply_manual_overrides() below, which
    handles that category structurally so it doesn't need weekly upkeep.
    """
    try:
        with open(path) as f:
            data = json.load(f)
    except FileNotFoundError:
        print(f"  [overrides] {path} not found -- skipping corrections/exclusions.")
        return {}, set()
    except json.JSONDecodeError as e:
        # A hand-edited overrides file with a stray comma/brace should
        # never cost you the whole scrape (this used to crash main()
        # right after scraping, before any output files got written --
        # see the [TIMING] line in the traceback that caused this fix).
        # Skip corrections/exclusions for this run instead and say
        # exactly where to look, rather than losing everything.
        print(f"  [overrides] WARNING: {path} is not valid JSON ({e}). "
              f"Skipping corrections/exclusions for this run -- fix the "
              f"file (check for a stray comma or brace near that line) "
              f"and re-run to pick them back up.")
        return {}, set()
 
    corrections = {
        (c["date"], c["known_team"]): c["corrected_opponent"]
        for c in data.get("corrections", [])
    }
    exclusions = {
        (e["date"], e.get("team1"), e.get("team2"))
        for e in data.get("exclusions", [])
    }
    print(f"  [overrides] Loaded {len(corrections)} correction(s) and "
          f"{len(exclusions)} exclusion(s) from {path}")
    return corrections, exclusions
 
 
def apply_manual_overrides(all_games, corrections, exclusions):
    """
    Three passes over the >=0-classified game list, in order:
 
    1. Drop any game where NEITHER side resolved to a classifications.json
       team. These have no MSHSAA relevance at all (they come from some
       other section of the scoreboard page, not an actual Missouri
       school's game) and this rule keeps catching new ones automatically
       every week with no maintenance -- this is the fix for the "at
       least one side must be classified" requirement.
    2. Drop anything in the manual exclusion list (matched on the exact
       raw date/team1/team2 as scraped -- these are specific one-off bad
       or duplicate games that don't fit a general rule).
    3. Apply a manual correction to the unclassified side's name, if one
       is on file for (date, classified side's name). Applied
       unconditionally when matched -- if MSHSAA's site later fills in
       its own name for that slot, this will still overwrite it with the
       name you confirmed, which is the point (holds true across score
       updates by design). If that's ever NOT what you want for a given
       game, that's what the exclusion list is for instead.
    """
    def _norm(name):
        # Defensive: treats "" (what resolve_name_or_raw() now returns
        # for a still-TBD opponent slot) and the legacy literal "(, )"
        # text (from data scraped before that fix) the same way -- both
        # mean "no usable name" -- so exclusion keys match consistently
        # regardless of which era a given row was scraped in.
        if name is not None and (name == "" or re.fullmatch(r"\(\s*,\s*\)", name)):
            return None
        return name
 
    kept = []
    dropped_both_unclassified = 0
    dropped_excluded = 0
    corrected = 0
 
    for g in all_games:
        g["team1"] = _norm(g["team1"])
        g["team2"] = _norm(g["team2"])
        c1, c2 = g["team1_classified"], g["team2_classified"]
 
        if not c1 and not c2:
            dropped_both_unclassified += 1
            continue
 
        raw_key = (g["date"], g["team1"], g["team2"])
        if raw_key in exclusions:
            dropped_excluded += 1
            continue
 
        if c1 and not c2:
            fix = corrections.get((g["date"], g["team1"]))
            if fix is not None and fix != g["team2"]:
                g["team2"] = fix
                corrected += 1
        elif c2 and not c1:
            fix = corrections.get((g["date"], g["team2"]))
            if fix is not None and fix != g["team1"]:
                g["team1"] = fix
                corrected += 1
 
        kept.append(g)
 
    print(f"  [overrides] Dropped {dropped_both_unclassified} game(s) with no classified "
          f"side, {dropped_excluded} manually-excluded game(s); "
          f"applied {corrected} name correction(s). {len(kept)} games remain.")
    return kept
 
 
def load_schedule_score_corrections(path=MANUAL_OVERRIDES_PATH):
    """
    Loads the score_corrections list from the same overrides file used for
    name corrections/exclusions. Each entry fills in a still-missing score
    for one specific game (exact date + the two team names, order doesn't
    matter) that you already know the result of ahead of MSHSAA posting
    it themselves.
 
    Unlike name corrections (which apply forever, since an out-of-state
    opponent's real name will never come from classifications.json on its
    own), a score_corrections entry is intentionally NOT permanent: see
    apply_score_corrections() below -- it only fires while the scraped
    score is still null. Once MSHSAA posts their own score for that game,
    the live scraped value takes over automatically and the entry just
    sits there harmlessly (no need to remove it after the fact).
    """
    try:
        with open(path) as f:
            data = json.load(f)
    except FileNotFoundError:
        return []
    except json.JSONDecodeError:
        # Same reasoning as load_manual_overrides() above -- a broken
        # overrides file shouldn't take the whole run down with it.
        # load_manual_overrides() already prints the warning for this
        # file, so this just quietly degrades rather than warning twice.
        return []
    return data.get("score_corrections", [])
 
 
def apply_schedule_score_corrections(all_games, score_corrections):
    """
    For every game whose score1 AND score2 are both still None, checks it
    against the manual score_corrections list on (date, the unordered
    pair of team names) -- matched by NAME rather than team1/team2
    position, so this stays correct even if team1/team2 end up swapped
    between scrape runs (the same swap issue apply_manual_overrides()
    already has to account for). If a game already has a score from the
    site, it's left alone -- the live scraped score always wins over a
    manual one, by design.
    """
    if not score_corrections:
        return all_games
 
    index = {}
    for sc in score_corrections:
        key = (sc["date"], frozenset([sc["team1"], sc["team2"]]))
        index[key] = sc
 
    applied = 0
    for g in all_games:
        if g["score1"] is not None or g["score2"] is not None:
            continue  # site already has a score for this game -- it wins
        key = (g["date"], frozenset([g["team1"], g["team2"]]))
        sc = index.get(key)
        if sc is None:
            continue
        if g["team1"] == sc["team1"]:
            g["score1"], g["score2"] = sc["score1"], sc["score2"]
        else:
            g["score1"], g["score2"] = sc["score2"], sc["score1"]
        applied += 1
 
    print(f"  [overrides] Filled in {applied} manually-provided score(s) "
          f"for game(s) MSHSAA hasn't posted a result for yet.")
    return all_games
 
 
def strict_games_from_all(all_games):
    """
    Filters the full (>=1 classified team) game list down to games where
    BOTH teams are in classifications.json, and reshapes each record back
    to the original schema (no *_classified fields) so this stays a
    drop-in replacement for whatever already consumes boys_soccer_games_2026.json.
    """
    strict = []
    for g in all_games:
        if not (g["team1_classified"] and g["team2_classified"]):
            continue
        strict.append({
            "date": g["date"],
            "team1": g["team1"],
            "score1": g["score1"],
            "team2": g["team2"],
            "score2": g["score2"],
            "forfeit": g["forfeit"],
            "overtime": g["overtime"],
        })
    return strict
 
 
def save_games_json(games, path=OUTPUT_JSON):
    # Internal "_" fields (e.g. _final) are never written out.
    clean = [{k: v for k, v in g.items() if not k.startswith("_")} for g in games]
    with open(path, "w") as f:
        json.dump(clean, f, indent=2)
    print(f"Saved {len(clean)} games to {path}")
 
 
def save_games_csv(all_games, path=OUTPUT_CSV):
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["date", "team1", "score1", "team2", "score2", "forfeit", "overtime"])
        for g in all_games:
            writer.writerow([g["date"], g["team1"], g["score1"], g["team2"], g["score2"],
                              g["forfeit"], g["overtime"]])
    print(f"Saved {len(all_games)} games to {path}")
 
 
def save_games_csv_all(all_games, path=OUTPUT_CSV_ALL):
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["date", "team1", "team1_classified", "score1",
                          "team2", "team2_classified", "score2",
                          "forfeit", "overtime"])
        for g in all_games:
            writer.writerow([g["date"], g["team1"], g["team1_classified"], g["score1"],
                              g["team2"], g["team2_classified"], g["score2"],
                              g["forfeit"], g["overtime"]])
    print(f"Saved {len(all_games)} games to {path}")
 
 
def rated_games_from_all(all_games):
    """
    Pick out the games the ratings engine uses, as (date, team1, score1,
    team2, score2) tuples: both teams classified, both scores posted,
    marked Final, not a forfeit, and no score above RATINGS_MAX_POINTS --
    the same rules the old ratings-only scrape applied.
    """
    rated = []
    skipped = Counter()
    for g in all_games:
        if not (g["team1_classified"] and g["team2_classified"]):
            continue
        if g["score1"] is None or g["score2"] is None:
            continue
        if g["forfeit"]:
            skipped["forfeit"] += 1
            continue
        if not g.get("_final", True):
            skipped["scored but not marked Final"] += 1
            continue
        if max(g["score1"], g["score2"]) > RATINGS_MAX_POINTS:
            skipped[f"score above {RATINGS_MAX_POINTS}"] += 1
            continue
        rated.append((g["date"], g["team1"], g["score1"], g["team2"], g["score2"]))
 
    print(f"  {len(rated)} completed game(s) between classified teams go to the ratings.")
    for reason, n in skipped.items():
        print(f"  Not rated ({reason}): {n}")
    return rated
 
 
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
# Converts the flat games list (team1/team2/score1/score2, one row per game)
# into the per-team schedule file the Sport Detail snippet reads:
#   {"season": ..., "generated": ..., "teams": {schoolName: [game, ...]}}
# Built straight from the scraped games (same list saved as the _all
# files), so no games file has to be read back in.
#
# Ratings-dependent fields (predicted_team_score, predicted_opp_score,
# off_delta, def_delta, ovr_delta) are still written as null, same as the
# standalone script.
# home_away is null because the source file has no home/away indicator;
# the front-end falls back to "at".
# "forfeit" and "overtime" are both carried through from the source file.
 
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
    print(f"=== MSHSAA Boys Soccer {SEASON_YEAR}: games, schedule, ratings ===")
 
    print("\nLoading classifications...")
    team_to_class, team_to_district = load_classifications()
    known_teams = set(team_to_class.keys())
    print(f"  Loaded {len(team_to_class)} teams from {CLASSIFICATIONS_PATH}")
 
    print("\nBuilding school ID → classification name lookup...")
    id_to_classname = build_id_to_classname(team_to_class, SCHOOLS_CSV)
 
    print(f"\nScraping {SEASON_START} to {SEASON_END}...")
    raw_games = scrape_full_season(id_to_classname, known_teams)
    print(f"\nTotal games found (before overrides/dedup, >=1 classified team): "
          f"{len(raw_games)}")
    if not raw_games:
        # Exit before writing anything, so a failed scrape can't overwrite
        # good files with empty ones.
        print("No games found -- exiting without writing any files.")
        exit(1)
 
    # Overrides run BEFORE dedup: MSHSAA sometimes lists a game twice with
    # team1/team2 swapped, and each raw copy needs its own chance to match
    # a correction/exclusion before dedup keeps one of them.
    print("\nApplying manual name corrections/exclusions...")
    corrections, exclusions = load_manual_overrides()
    raw_games = apply_manual_overrides(raw_games, corrections, exclusions)
 
    print("\nApplying manual score fills...")
    raw_games = apply_schedule_score_corrections(
        raw_games, load_schedule_score_corrections())
 
    print("\nDeduplicating games...")
    schedule_games = deduplicate_schedule_games(raw_games)
    strict_games = strict_games_from_all(schedule_games)
    print(f"Of those, {len(strict_games)} have both teams classified "
          f"({len(schedule_games) - len(strict_games)} have exactly one classified side).")
 
    print("\nSaving games files...")
    save_games_json(strict_games, OUTPUT_JSON)
    save_games_csv(strict_games, OUTPUT_CSV)
    save_games_json(schedule_games, OUTPUT_JSON_ALL)
    save_games_csv_all(schedule_games, OUTPUT_CSV_ALL)
 
    print("\nBuilding schedule files...")
    save_schedule(build_schedule(schedule_games))
 
    print("\nSelecting completed games for ratings...")
    all_games = rated_games_from_all(schedule_games)
    if not all_games and not MANUAL_GAMES:
        print("No completed games yet -- games/schedule files saved, "
              "skipping ratings.")
        exit(0)
 
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
 
    report_teams_not_rated(ovr_rating, team_to_class)
 
    print("\n=== Done ===")
 
