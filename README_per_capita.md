# Size-Adjusted ("Per Capita") Rankings — setup

One shared script (`build_per_capita_2026.py`) serves every sport. The sport is
chosen with `--sport`.

## Files for each repo

| Repo | Repo root | `.github/workflows/` |
|---|---|---|
| `Fall_Softball_Ratings_2026` | `build_per_capita_2026.py`, `mshsaa_enrollments_2026-2027.json` | `build_per_capita_fall_softball_2026.yml` |
| `Boys_Soccer_Ratings_2026` | `build_per_capita_2026.py`, `mshsaa_enrollments_2026-2027.json` | `build_per_capita_boys_soccer_2026.yml` |
| `Girls_Volleyball_Ratings_2026` | `build_per_capita_2026.py`, `mshsaa_enrollments_2026-2027.json` | `build_per_capita_girls_volleyball_2026.yml` |

`build_per_capita_2026.py` and `mshsaa_enrollments_2026-2027.json` are the same
in every repo. If you change one copy, copy the change to the others.

The first push of the script and enrollment file triggers a run, which commits
`per_capita_<sport>_2026.json` and `.csv`. After that, each workflow runs
automatically when that sport's nightly ratings workflow finishes.

## Running locally

```
pip install numpy
python build_per_capita_2026.py --sport fall_softball
python build_per_capita_2026.py --sport boys_soccer
python build_per_capita_2026.py --sport girls_volleyball
```

Run it from the sport's repo folder. If the ratings or enrollment file isn't
next to the script, it falls back to the GitHub raw URL.

## Settings (top of the script)

- **`SPORTS[...]["private_factor"]`**: the multiplier applied to private-school
  enrollment. 2026 values:

  | Sport | Measured | Used |
  |---|---|---|
  | Football | 2.17 | 2.2 |
  | Boys Soccer | 2.98 | 3.0 |
  | Girls Volleyball | 2.98 | 3.0 |
  | Fall Softball | 0.88 | 1.0 (no adjustment) |

  Each run prints the current measured value next to the one in use. Set a
  sport to `"auto"` to re-measure it on every run.
- **`PRIVATE_SCHOOLS` / `CHARTER_SCHOOLS`**: who gets the factor. Charters
  always use raw enrollment. Public magnet schools (Metro, Lincoln College
  Prep, Paseo) count as public.
- **`COOP_COMBINE`**: co-ops add up their members' enrollments. Each member is
  scaled by its own factor, so a public + private co-op gets a blended factor.
- **`ENROLLMENT_OVERRIDES`**: patch a single school's enrollment.

## Football

The same script also runs football: `--sport football` gives the same results
as `build_per_capita_football_2026.py`. To switch football over, put
`build_per_capita_2026.py` in `football-ratings-2026` and change the workflow's
run line to `python build_per_capita_2026.py --sport football`.
