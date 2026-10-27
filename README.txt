All MO Sports - Matchup Previews for every fall sport (v12)

Each folder mirrors a GitHub repo. Upload its files to the same paths.

NEW
  Girls_Volleyball_Ratings_2026/  builder + workflow + snippet
  Fall_Softball_Ratings_2026/     builder + workflow + snippet

UPDATED
  Boys_Soccer_Ratings_2026/build_matchup_previews_boys_soccer_2026.py
      now the shared builder (same output as before; only the SPORT line differs per repo)
  */wpcode/*_snippet.html
      all four snippets are v12 - identical code, only `var SPORT = "...";` differs

AFTER UPLOADING
  1. Run "Build Matchup Previews (Girls Volleyball)" and "(Fall Softball)" once from the Actions tab.
     After that they rebuild every time that sport's nightly ratings workflow finishes.
  2. Make a WordPress page per sport and paste its snippet (WPCode HTML snippet, shortcode).
  3. Replace the football and soccer snippets on their pages with the v12 copies.
