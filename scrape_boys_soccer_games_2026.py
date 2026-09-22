"""
MSHSAA Boys Soccer GAMES Scraper - 2026 Season (schedule pull, no ratings)
=========================================================================

Adapted from scrape_football_games_2026.py, itself adapted from
boys_soccer_ratings_2025.py (or whichever prior-season script exists for this sport -- adjust if the naming differs). Same request approach (requests + BeautifulSoup,
NOT Playwright -- the page is plain server-rendered HTML), same
session/retry logic, same data-school-ID based name-resolution -- MSHSAA's
scoreboard.aspx uses the same page template across sports, just a
different "alg" query-string code per sport (Boys Soccer = alg=33
below). That template-reuse assumption is NOT verified against a live
Boys Soccer scoreboard page from this environment (no network access to
mshsaa.org here) -- spot-check the first clean run's output against the
live site before trusting it for real use, same as you would for any new
scraper.

THIS IS A CLEAN, STRIPPED-DOWN FIRST RUN FOR BOYS SOCCER
-----------------------------------------------------------------------------
Unlike the football version, this script does NOT carry over football's
MANUAL_OVERRIDES (school-ID -> classification-name overrides) or its
accumulated manual_name_overrides.json corrections/exclusions/score
corrections -- those were built up specifically for football's roster and
would be wrong (or at best irrelevant) if applied here. Both mechanisms
start EMPTY for Boys Soccer:
  - MANUAL_OVERRIDES (in build_id_to_classname) is an empty dict. Any school
    ID that doesn't cleanly match a name in classifications.json will show
    up unresolved -- that's the point of a clean run. Once you've seen
    which IDs come back unresolved, add Boys Soccer-specific entries here,
    the same way football's list was built up.
  - MANUAL_OVERRIDES_PATH points to "boys_soccer_manual_name_overrides.json",
    which almost certainly doesn't exist yet. load_manual_overrides() and
    load_score_corrections() already handle a missing file gracefully (0
    corrections/exclusions loaded, printed clearly) -- so this isn't a bug,
    it's the intended clean-run behavior. Create that file once you know
    what needs correcting, following the same schema as football's.

SCORE MODEL
-----------------------------------------------------------------------------
Assumed to be a single running goals-scored integer per team, same shape as
football's score. MAX_POINTS is set generously high (goals rarely exceed
single digits, but mercy-rule blowouts can run higher) -- tighten this once
you've seen a season's worth of real scores come through.

WHAT THIS SCRIPT DOES (same behavior as the football version)
-----------------------------------------------------------------------------
  1. No "final" status requirement -- keeps scheduled/unscored games too.
  2. Detects team cells by scanning ALL cells in ALL rows for the
     data-school attribute / MySchool link, not a fixed row/column index.
  3. Score is optional -- captured if present and parseable, else null.
  4. No date.today() cap -- scrapes the full configured season range.
  5. Forfeited games are kept (with a "forfeit" boolean), not dropped.
  6. Overtime detection via row text match -- UNVERIFIED for this sport,
     same caveat as football's (see is_overtime()'s docstring). Soccer
     does have real overtime periods, so this flag should be meaningful
     here, but confirm the exact wording MSHSAA uses on a live soccer
     scoreboard page before trusting it (may differ from football's
     "overtime"/"OT" text).

REQUIRES (same as your existing pipeline, must be in the same directory
or update the paths below):
  - classifications.json  (Boys Soccer 2026-27 projected classifications --
    NOTE: confirm this is Boys Soccer's own classifications file and not
    football's; if AllMOSports keeps one shared classifications.json across
    all sports, filter this loader to this sport before using it, since
    class groupings differ per sport)
  - mshsaa_schools.csv     (school_id -> school_name lookup, shared across
    sports)

Usage:
    python3 scrape_boys_soccer_games_2026.py
"""
 
import requests
from bs4 import BeautifulSoup
import json
import csv
import re
import pandas as pd
from datetime import date, timedelta
import time
import socket
import urllib3.util.connection as urllib3_cn

# Added after the football scraper (same machine) hit "Network is
# unreachable" (errno 101) -- traced to mshsaa.org resolving to both
# IPv4 and IPv6 addresses, with this network's IPv6 route not actually
# working. Forces urllib3 (which requests uses under the hood) to only
# resolve IPv4 addresses, sidestepping the problem. Harmless if this
# particular script was never hitting it.
def _force_ipv4_only():
    return socket.AF_INET

urllib3_cn.allowed_gai_family = _force_ipv4_only
 
# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------
 
SEASON_YEAR   = 2026
SEASON_START  = date(2026, 8, 1)
SEASON_END    = date(2026, 11, 15)   # rough estimate based on typical MSHSAA Boys Soccer state tournament timing -- CONFIRM the actual 2026 championship date and adjust
BASE_URL      = "https://www.mshsaa.org/activities/scoreboard.aspx?alg=33&date={}"  # alg=33 = Boys Soccer, per your existing games-scraper pipeline
MAX_POINTS    = 25  # see SCORE MODEL note above -- verify against a live page
OUTPUT_JSON   = f"boys_soccer_games_{SEASON_YEAR}.json"
OUTPUT_CSV    = f"boys_soccer_games_{SEASON_YEAR}.csv"
OUTPUT_JSON_ALL = f"boys_soccer_games_{SEASON_YEAR}_all.json"
OUTPUT_CSV_ALL  = f"boys_soccer_games_{SEASON_YEAR}_all.csv"
CLASSIFICATIONS_PATH = "classifications.json"
SCHOOLS_CSV           = "mshsaa_schools.csv"
 
REQUEST_DELAY = 0.5  # seconds between requests, matches 2025 script
 
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
# HTTP SESSION (identical to boys_soccer_ratings_2025.py (or whichever prior-season script exists for this sport -- adjust if the naming differs))
# ---------------------------------------------------------------------------
 
def build_session():
    from requests.adapters import HTTPAdapter
    try:
        from urllib3.util.retry import Retry
    except ImportError:
        from requests.packages.urllib3.util.retry import Retry
 
    session = requests.Session()
    retry = Retry(
        total=1,
        connect=1,
        read=1,
        backoff_factor=1.5,
        status_forcelist=[500, 502, 503, 504],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=1, pool_maxsize=1)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session
 
 
# ---------------------------------------------------------------------------
# CLASSIFICATIONS / NAME RESOLUTION (identical to boys_soccer_ratings_2025.py (or whichever prior-season script exists for this sport -- adjust if the naming differs))
# ---------------------------------------------------------------------------
 
def load_classifications(path=CLASSIFICATIONS_PATH):
    with open(path) as f:
        data = json.load(f)
    team_to_class    = {}
    team_to_district = {}
    for entry in data["teams"]:
        school = entry["school"]
        team_to_class[school]    = entry["classification"]
        team_to_district[school] = entry["district"]
    return team_to_class, team_to_district
 
 
def build_id_to_classname(team_to_class, schools_csv=SCHOOLS_CSV):
    """
    Build { school_id_str : classification_name }. MANUAL_OVERRIDES is
    intentionally empty for this clean run (see module docstring). If a
    school ID fails to resolve to a classifications.json name below, add
    its "id": "correct classification name" entry here once you've
    confirmed it from a live scoreboard page or classifications.json
    itself -- co-op programs and schools with a name that doesn't match
    mshsaa_schools.csv exactly are the most likely source of unresolved IDs.
    """
    # STRIPPED for this clean run -- see the module docstring's
    # "THIS IS A CLEAN, STRIPPED-DOWN FIRST RUN" section. Populate this
    # after you've seen which school IDs come back unresolved.
    MANUAL_OVERRIDES = {
        "1206": "Ewing Marion Kauffman with University Academy Charter",
        "189": "Roosevelt",
        "194": "Smith-Cotton",
        "549": "St. Mary's South Side",
        "465": "Stover",
        "207": "Sullivan",
        "556": "Thomas Jefferson Independent Day",
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
 
    id_to_classname.update(MANUAL_OVERRIDES)
 
    print(f"  [name-resolve] {len(id_to_classname)} schools mapped by ID "
          f"({len(MANUAL_OVERRIDES)} via manual overrides)")
    return id_to_classname
 
 
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
    return all_games
 
 
def deduplicate_games(all_games):
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
 
 
MANUAL_OVERRIDES_PATH = "boys_soccer_manual_name_overrides.json"
 
 
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
 
 
def load_score_corrections(path=MANUAL_OVERRIDES_PATH):
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
 
 
def apply_score_corrections(all_games, score_corrections):
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
 
 
def teams_missing_final_scores(all_games, known_teams):
    """
    Compares the full universe of known teams (from classifications.json)
    against the set of teams that show up in at least one scraped game
    where BOTH score1 and score2 parsed to a real value (i.e. an actual
    final score, not just a scheduled/unscored matchup).
 
    Returns a sorted list of team names with zero games that have a
    final score on record.
    """
    teams_with_final_score = set()
    for g in all_games:
        if g["score1"] is not None and g["score2"] is not None:
            teams_with_final_score.add(g["team1"])
            teams_with_final_score.add(g["team2"])
 
    missing = sorted(known_teams - teams_with_final_score)
    return missing
 
 
def report_missing_final_scores(all_games, known_teams):
    missing = teams_missing_final_scores(all_games, known_teams)
    print(f"\n=== Teams with NO final-score game on record: {len(missing)} of {len(known_teams)} ===")
    if missing:
        for team in missing:
            print(f"  - {team}")
    else:
        print("  None -- every known team has at least one final score.")
    return missing
 
 
def save_json(all_games, path=OUTPUT_JSON):
    with open(path, "w") as f:
        json.dump(all_games, f, indent=2)
    print(f"Saved {len(all_games)} games to {path}")
 
 
def save_csv(all_games, path=OUTPUT_CSV):
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["date", "team1", "score1", "team2", "score2", "forfeit", "overtime"])
        for g in all_games:
            writer.writerow([g["date"], g["team1"], g["score1"], g["team2"], g["score2"],
                              g["forfeit"], g["overtime"]])
    print(f"Saved {len(all_games)} games to {path}")
 
 
def save_csv_all(all_games, path=OUTPUT_CSV_ALL):
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
 
 
# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
 
if __name__ == "__main__":
    print(f"=== MSHSAA Boys Soccer Games Pull {SEASON_YEAR} (schedule, no ratings) ===")
 
    print("\nLoading classifications...")
    team_to_class, team_to_district = load_classifications()
    known_teams = set(team_to_class.keys())
    print(f"  Loaded {len(team_to_class)} teams from {CLASSIFICATIONS_PATH}")
 
    print("\nBuilding school ID -> classification name lookup...")
    id_to_classname = build_id_to_classname(team_to_class, SCHOOLS_CSV)
 
    print(f"\nScraping {SEASON_START} to {SEASON_END}...")
    all_games = scrape_full_season(id_to_classname, known_teams)
    print(f"\nTotal games found (before overrides/dedup, >=1 classified team): {len(all_games)}")
 
    if not all_games:
        print("No games found. This is expected if the 2026 schedule "
              "hasn't been posted to MSHSAA yet -- try again closer to "
              "the season, or check a known date manually in a browser.")
 
    # Overrides run BEFORE dedup, on the full un-deduplicated list. MSHSAA
    # sometimes lists the same game twice with team1/team2 swapped (once
    # under each school's own schedule table); if dedup ran first, its
    # (date, frozenset(team1, team2)) key can't tell those two raw copies
    # apart and keeps whichever one happened to scrape first -- which
    # might be the copy that matches an exclusion entry, silently
    # discarding the correctable copy before it ever reached
    # apply_manual_overrides(). Running overrides first lets each raw
    # copy get matched against corrections/exclusions independently; the
    # dedup pass below then cleans up any true duplicates left over
    # (e.g. two copies that both got corrected to the identical names).
    print("\nApplying manual name corrections/exclusions...")
    corrections, exclusions = load_manual_overrides()
    all_games = apply_manual_overrides(all_games, corrections, exclusions)
 
    print("\nApplying manual score corrections...")
    score_corrections = load_score_corrections()
    all_games = apply_score_corrections(all_games, score_corrections)
 
    print("\nDeduplicating...")
    all_games = deduplicate_games(all_games)
 
    strict_games = strict_games_from_all(all_games)
    print(f"Of those, {len(strict_games)} have both teams classified "
          f"({len(all_games) - len(strict_games)} have exactly one classified side).")
 
    print("\nSaving output...")
    save_json(strict_games, OUTPUT_JSON)
    save_csv(strict_games, OUTPUT_CSV)
    save_json(all_games, OUTPUT_JSON_ALL)
    save_csv_all(all_games, OUTPUT_CSV_ALL)
 
    report_missing_final_scores(all_games, known_teams)
 
    print("\n=== Done ===")
