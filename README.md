# Sports Research Dashboard

A free, private, local tool for researching NBA player props, NBA game
markets, and CS2 / Counter-Strike 2 matches.

No paid APIs required. No sportsbook integration. No cloud. Runs on your laptop.

---

## What this app does

1. You pick an NBA player and a season.
2. You pick a stat (Points, Rebounds, Assists, PRA, etc.).
3. You type the posted prop line (like `24.5`).
4. The app shows:
   - A **projection** (its guess for tonight) based on recent and season form.
   - An **edge** = projection minus line.
   - An **over/under lean** based only on the projection.
   - A **hit rate** on the last 10 games vs the line.
   - Game logs, home/away splits, win/loss splits, rest-day splits,
     history vs the opponent, and trend charts.
   - A **plain-English explanation** of how the projection was built.

The projection is intentionally simple and inspectable — no black box.

There is also a second top-level mode — **Game Markets** — for
matchup-level totals / team totals / spreads, including 1st-half and
2nd-half markets. See the **Game Markets mode** section below.

A third top-level mode — **CS2** — adds a separate Counter-Strike 2
research dashboard. The primary data source is the
[gigobyte/HLTV](https://github.com/gigobyte/HLTV) Node.js bridge — free,
no API key required — backed by an aggressive cache/throttle/circuit-
breaker stack to stay polite with HLTV. [GRID Open Access](https://grid.gg/)
is preserved as an experimental future placeholder (disabled by default).
See the **CS2 mode** section below for setup and details.

---

## Filter-Driven Research Dashboard

On top of the classic five tabs, the app now has a **Research** tab (the
default view) driven by a full filter modal. Open **Filters** from the
top bar to narrow the sample by season, home/away, H2H, back-to-back,
teammate WITH/WITHOUT, opponent rank, stat ranges, and more. Every
active filter recomputes:

- Main bar chart (green = over line, red = under line, gold prop line)
- Hit rate + sample size
- Filtered average vs excluded average diffs
- Stat-card grid (PTS / AST / REB / 3PM / MIN / FGA / USG proxy)
- Teammate WITH / WITHOUT cards

### Filter order of operations

The filter pipeline (`services/filter_service.py`) applies filters in a
fixed order so the behaviour is deterministic:

1. **Season** — (no-op for single-season logs; hook for "All" merging)
2. **Regular / Playoffs** — uses SEASON_TYPE
3. **Home / Away, Win / Loss, H2H, B2B, Rest, Margin** — narrow the sample
4. **Teammate WITH / WITHOUT** — intersect with each teammate's game-id set
5. **Opp-rank chips** — narrow to games vs. top-10 "easiest" defences
6. **Stat ranges** — per-column min/max from the Stats tab
7. **Game count** — cap to last N (applied last so "last 10" reflects
   the already-narrowed sample)

The `excluded` sample used for diffs is everything removed by steps 2–6
(the game-count cap is *not* considered an exclusion because those games
still match every filter).

### Supported vs beta filters

Some advanced angles on commercial prop tools aren't available from
free nba_api endpoints. We render them so the product surface is
complete, but toggling a beta chip has no effect on the sample. They
are visibly labelled **· beta** and have a tooltip explaining why.

| Section | Real | Beta |
|---|---|---|
| Season & Games | Season select, game count, lock, custom N | — |
| Suggested | Minutes ≥ L10 avg, Home/Away, H2H, B2B, Opp rank chips (PTS/REB/AST/3PM/STL/BLK/TOV/Pace/DefRtg) | Def vs Position, Def vs DPT/CSU/Pull-Up, Opp 3PA allowed |
| Opp Rankings → Team Defense | Opp PTS/REB/AST/3PM/STL/BLK/TOV allowed, Opp Pace, Opp DefRtg | Paint PTS allowed, 3PA allowed, FTA allowed |
| Opp Rankings → Play Types | — | Spot Up, Transition, PnR BH/RM, Iso, Handoff, Putbacks, Post Up, Off Screen |
| Opp Rankings → Shooting Zones | (live when LeagueDashTeamShotLocations returns) | Restricted Area, Paint non-RA, Mid, ATB3, LC3, RC3 |
| Opp Rankings → Assist Zones | — | DAZ, DAZ2 |
| Splits | H2H, Home, Away, Regular, Playoffs, B2B, Win/Loss, Close/Blowout margin, Rest range | Closing Spread, Closing Total |
| Stats | Avg/Median toggle, 14 stat overlay chips, per-stat range filters | — |
| Teammates | WITH / WITHOUT / Off (multi-select), manual availability tag, per-market diff | Automatic injury auto-tagging |

### Understanding Filters (quick reference)

- **Sample filtering.** Every filter narrows — never grows — the set of
  games the UI inspects. Clearing filters restores the full log.
- **Red vs green matchup ranking.** A badge like `#5/30` means the
  opposing defence ranks 5th easiest for that stat. Green = top third,
  red = bottom third, grey = middle.
- **Avg vs Median toggle.** Controlled from the Stats tab. Median is
  more robust to outlier games.
- **Teammate WITH / WITHOUT.** WITH keeps only games where both players
  played; WITHOUT keeps only games where the teammate did not play.
- **Δ vs excluded.** Each stat card shows the filtered-sample value
  compared against the games that didn't match the filter. Big positive
  diffs mean the filter is capturing "hot" games.

### Implementation notes

- The UI state lives in `ui/filter_state.py::FilterState` held in
  `st.session_state["filter_state"]`.
- The pipeline (`services/filter_service.py::apply_all_filters`) is a
  pure function and fully unit-testable.
- Rank maps (`services/rankings_service.py`) use the same SQLite cache
  as the rest of the app (`data/cache.sqlite`). Shot-location fetches
  wrap in try/except so a 403 from stats.nba.com gracefully degrades
  shooting-zone filters to the beta surface.
- Teammate game-ID sets are derived by intersecting the teammate's own
  game log with the player's — no BoxScore endpoint needed, so the
  first-time fetch for a teammate takes the same time as loading one
  extra player season.

### Game Markets mode

The sidebar has a top-level **Mode** selector — Player Props (the default)
or **Game Markets**. Game Markets is a separate research workflow for
matchup-level markets:

- Full Game Total
- Team Total (either side)
- Full Game Spread (either side)
- 1st Half Total · 1st Half Spread
- 2nd Half Total · 2nd Half Spread

You pick two teams, a season, a market, a side where relevant, and type
the posted line. The model returns a transparent projection, an edge,
and an over/under or cover lean with a plain-English breakdown.

**Data model (all free, local-cached):**

| Piece | Source | Cache |
|---|---|---|
| Team game logs (PTS, PLUS_MINUS, matchup…) | `TeamGameLog` via `services/team_service.py` | SQLite namespace `team_gamelog`, 6 h TTL, stale fallback |
| PACE / DEF_RATING / NET_RATING | `LeagueDashTeamStats(Advanced)` via `services/rankings_service.py` | SQLite namespace `rankings`, 12 h TTL |
| Upcoming schedule (optional) | `ScoreboardV2` via `services/matchup_service.py` | Shares `schedule` namespace with player-props mode |
| Per-game 1H / 2H points (**beta**) | `BoxScoreSummaryV2.LineScore` via `services/linescore_service.py` | SQLite namespace `linescore`, permanent |

**Projection math (explicit, no black box):**

- **Full game total:** `proj_A = 0.5 × (A_off + B_def)`, `proj_B = 0.5 × (B_off + A_def)`,
  combined total scaled by `pace_factor = (A_pace + B_pace) / (2 × league_pace)`
  clamped to ±10 %. Last-10 games are used for the offence/defence baselines.
- **Team total:** same formula applied to the selected side only.
- **Full game spread:** NET_RATING difference + a 30 %-weighted recent-form delta
  (each team's last-10 margin minus its own net rating) + a fixed
  `HOME_COURT_ADV = 2.5` when the selected team is home.
- **1H / 2H totals and spreads:** when per-game line scores are loaded, averages
  over the last 10 games of `TOTAL_1H / TOTAL_2H / MARGIN_1H / MARGIN_2H`. Otherwise
  a heuristic 48 % / 52 % share of the full-game projection (marked
  **· beta** in the UI).

**Half-level data loading (beta):** the default projection for the four
half markets uses the heuristic shares. Click **Load half-level data
(beta)** above the summary cards to run `BoxScoreSummaryV2` once per
recent game for both teams; this is one API call per game (20 total for
the default 10-game window). Results are permanently cached, so later
sessions are instant.

**Cover convention:** a team posted at `-4.5` covers when the projected
margin is greater than `4.5`. A team posted at `+3.0` covers when the
projected margin is greater than `-3.0` (any loss smaller than 3 still
covers). The projection service expresses this as `covers_if_gt =
-manual_spread`.

**What the mode does NOT do:**

- No automatic line pulling from sportsbooks — you type it.
- No paid feeds for injuries or pace-adjusted defensive ratings.
- No betting-edge probability or Kelly sizing — the lean is plain
  projection vs line.
- No playoff series-level adjustments; game-level only.

### CS2 mode

A separate top-level research dashboard for Counter-Strike 2, selected
from the same sidebar **Mode** radio. CS2 is a fully isolated product
area — its own `services/cs2/` namespace, its own UI modules, its own
disk cache. Nothing in the NBA code paths runs while CS2 is active.

**Provider priority:**

| Priority | Provider | Requires |
|---|---|---|
| 1 (primary, default) | **HLTV** via gigobyte/HLTV Node.js bridge — unofficial scraper | Node.js ≥ 16 + `npm install` |
| 2 (fallback) | **Mock** — static demo data, always available | nothing |
| 3 (experimental / future) | **GRID Open Access** — structured API; only if `CS2_PROVIDER=grid` | `GRID_API_KEY` + Central Data access |

The app resolves the provider on every call. With the default
`CS2_PROVIDER=hltv`, GRID code is never invoked. GRID is preserved as a
placeholder so the project can switch back later if access improves.

**What the mode covers (HLTV-backed):**

| Tab | What you get |
|---|---|
| **Overview** | Recent results; auto-loads one API call on tab open. Upcoming matches load on demand. |
| **Matches** | Filtered result list from session cache; click any match for per-map detail (lazy). |
| **Players** | Free-text player search; profile + on-demand stats (K/D, ADR, HS%, Rating, opening duels, per-map history). |
| **Teams** | Free-text team search; profile + roster + recent form + map-pool win rates. |
| **Maps** | Player kills by map; team-vs-team map pool comparison. Pure derivation from already-loaded session data — zero new requests. |
| **Tournaments** | Not supported on the HLTV primary (gigobyte/hltv has no event list endpoint). Tab shows a clear notice instead. Only useful if you switch to the experimental GRID path. |

**Data availability by provider:**

| Metric | HLTV (primary) | GRID (experimental) |
|---|---|---|
| Match schedule / results | ✅ | ✅ |
| Series / match detail | ✅ | ✅ basic |
| Tournament list | ❌ | ✅ |
| Free-text player / team search | ✅ | ❌ |
| Team / player profiles | ✅ | ✅ |
| K/D ratio | ✅ | ⚠️ pending audit |
| Average Damage per Round (ADR) | ✅ | ❌ |
| Headshot % | ✅ | ⚠️ pending audit |
| HLTV Rating 2.0 | ✅ | ❌ |
| Opening kills / deaths | ✅ | ❌ |
| Team map-pool win rates | ✅ | ❌ |
| Team world ranking | ✅ | ❌ |
| KAST % | ❌ | ❌ (not in gigobyte/hltv schema) |
| T-side / CT-side splits | ❌ | ❌ (requires deeper scrape) |

> **Note on HLTV reliability.** gigobyte/HLTV is an unofficial
> community library. It is subject to HLTV site redesigns and to
> Cloudflare bot-protection on data-center / VPN IPs. The anti-ban
> stack below keeps usage polite, and the UI degrades to stale cache
> + a calm notice when live fetches fail.

---

### CS2 setup

#### Option A — HLTV primary (recommended, default)

1. Install Node.js ≥ 16 (e.g. via `brew install node` or `nvm`).
2. Install the bridge (one-time):

```bash
cd services/cs2/hltv_bridge
npm install
cd ../../..
```

3. Copy `.env.example` to `.env` if you haven't already:

```bash
cp .env.example .env
```

Leave `CS2_PROVIDER=hltv` (the default). No API key is required.

4. Run the app:

```bash
streamlit run app.py
```

Select **CS2** from the sidebar. The status panel shows HLTV bridge
state, last sync time, throttle window, and circuit-breaker status.

#### Option B — Mock only (no network, development)

Set `CS2_USE_MOCK_FALLBACK=true` in `.env`, or simply skip the HLTV
bridge install. The tabs render against static demo data.

#### Option C — Experimental GRID path (for future use)

GRID Open Access *may* become useful if the free tier starts including
Central Data. Today it doesn't, so the app does not use it by default.
To try it:

1. Get a free GRID Open Access API key from [grid.gg](https://grid.gg/).
2. Add both of these to `.env`:

```bash
CS2_PROVIDER=grid
GRID_API_KEY=your_key_here
```

3. Use the **🔬 GRID Response Audit** panel that appears in the CS2
   sidebar (only shown when `CS2_PROVIDER=grid`) to inspect raw GRID
   responses. If GRID returns `PERMISSION_DENIED`, your tier does not
   include Central Data GraphQL — the app transparently falls back to
   HLTV for that session and shows a banner explaining the state.

---

### CS2 anti-ban architecture

HLTV (primary) and GRID (experimental) share the same protective layers:

| Protection | HLTV (primary) | GRID (experimental) |
|---|---|---|
| **Cache-first** | `data/cs2_cache/` (prefix `hltv:`) | `data/cs2_cache/` (prefix `grid:`) |
| **TTLs** | Results 6 h · stats 24 h · ranking 4 h | Series list 6 h · end-state 30 days · tournaments 24 h |
| **Stale fallback** | ✅ previous payload served on failure | ✅ |
| **Serialised requests** | Threading lock — one call at a time | Threading lock |
| **Throttle** | 3 s between calls (`CS2_HLTV_THROTTLE_SECONDS`) | 1 s between calls (`GRID_THROTTLE_SECONDS`) |
| **Circuit breaker** | 3 fails → 5-minute cooldown | — |
| **Lazy loading** | All expensive tabs behind explicit user buttons | Same |

**Cloudflare note.** From a data-center or VPN IP, HLTV often returns
a Cloudflare challenge (shown as `🛡️` in the UI). The app catches this
gracefully and serves stale cache. Home / residential IPs work
reliably.

**Configuration knobs** (set in `.env` or environment):

| Variable | Default | Purpose |
|---|---|---|
| `CS2_PROVIDER` | `hltv` | `"hltv"` (default), `"mock"`, or `"grid"` (experimental) |
| `CS2_USE_MOCK_FALLBACK` | `false` | Force demo data regardless of provider |
| `CS2_CACHE_DIR` | `data/cs2_cache/` | Shared disk cache directory |
| `CS2_HLTV_THROTTLE_SECONDS` | `3` | Min gap between HLTV requests |
| `CS2_HLTV_MAX_RETRIES` | `1` | Retries per HLTV call (keep low) |
| `CS2_HLTV_CB_THRESHOLD` | `3` | Failures before HLTV cooldown |
| `CS2_HLTV_COOLDOWN_SECONDS` | `300` | HLTV cooldown duration (seconds) |
| `CS2_HLTV_NODE_TIMEOUT` | `30` | Node.js subprocess timeout |
| `CS2_ENABLE_HLTV_ENRICHMENT` | `false` | Legacy: only meaningful when `CS2_PROVIDER=grid` |
| `GRID_API_KEY` | _(none)_ | Experimental: GRID Open Access API key |
| `GRID_CS2_TITLE_ID` | `4` | Experimental: CS2 title ID in GRID |
| `GRID_THROTTLE_SECONDS` | `1` | Experimental: min gap between GRID requests |
| `GRID_MAX_RETRIES` | `1` | Experimental: retries per GRID call |
| `PANDASCORE_TOKEN` | _(none)_ | Optional PandaScore supplement (schedule only) |

Copy `.env.example` to `.env` to set these without editing code.

**Known CS2 limitations:**

- gigobyte/HLTV is no longer actively maintained upstream; HLTV site
  redesigns may break parsing silently.
- Cloudflare blocks HLTV from data-center / VPN IPs; the app degrades
  gracefully to stale cache (with a calm `🛡️` banner) in that case.
- KAST%, T-side/CT-side splits, and pistol-round stats are not in the
  gigobyte/hltv schema — tabs show "unavailable" rather than faking
  data.
- The experimental GRID path requires Central Data GraphQL access,
  which is not currently included with the free GRID Open Access tier.
  If selected but denied, the app falls back to HLTV transparently.

### Filter-by-Teammate modal (limitations)

The teammate summary bar on the Research view opens a dedicated
**Filter by Teammate** modal (`ui/teammate_modal.py`) for scanning and
toggling WITH / WITHOUT in one place. A few things to know:

- **First-open latency.** The modal shows per-teammate minutes and
  market-stat averages, which requires one game log fetch per teammate
  on first open for a given team/season. Every call is SQLite-cached,
  so later opens are near-instant. Roster size (~15) × log fetch
  (~1–2s each cold) puts the first open around 15–30s.
- **Headshot CDN fallbacks.** Headshots are pulled from
  `https://cdn.nba.com/headshots/nba/latest/260x190/{player_id}.png`.
  Historical players sometimes 404; the row silently falls back to an
  initials bubble via an `onerror` handler so alignment is preserved.
- **Manual availability tags.** There is no free injury feed we trust,
  so the `OUT / GTD / QUES / ACTIVE` chip is a manual dropdown stored
  only in `FilterState.teammate_availability` (session state — cleared
  on page reload).
- **Per-teammate averages are season-wide**, not conditional on the
  selected player being on the floor. A true "with-me lineup" stat
  would need BoxScoreAdvanced or play-by-play, which is out of scope
  for the free tier. The values are still a useful relative scan.

---

## Step 1 — Install Python

You need **Python 3.10 or newer**.

Check what you have:

```bash
python3 --version
```

If that prints `3.10.x` or newer, you're good. Otherwise:

- **macOS**: install from [python.org/downloads](https://www.python.org/downloads/)
  or via Homebrew: `brew install python@3.12`.
- **Windows**: install from [python.org/downloads](https://www.python.org/downloads/)
  and tick "Add Python to PATH" during setup.
- **Linux**: use your package manager, e.g.
  `sudo apt install python3 python3-venv python3-pip`.

---

## Step 2 — Get the project

Open a terminal and navigate to the project folder (this folder).

On macOS/Linux:

```bash
cd /path/to/Outlier
```

On Windows (PowerShell):

```powershell
cd C:\path\to\Outlier
```

---

## Step 3 — Create a virtual environment

A virtual environment keeps this project's Python packages isolated from
the rest of your system. Do this once.

**macOS / Linux:**

```bash
python3 -m venv .venv
source .venv/bin/activate
```

**Windows (PowerShell):**

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
```

After activation, your terminal prompt should start with `(.venv)`.

---

## Step 4 — Install dependencies

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

This installs seven libraries: `streamlit`, `nba_api`, `pandas`, `numpy`,
`plotly`, `python-dateutil`, `requests`.

---

## Step 5 — Run the app

```bash
streamlit run app.py
```

You should see output like:

```
You can now view your Streamlit app in your browser.

  Local URL: http://localhost:8501
```

Open http://localhost:8501 in your browser. Done.

The sidebar exposes a **Mode** selector at the very top:

- **Player Props** — classic player + market + manual prop-line flow.
- **Game Markets** — matchup-level totals, team totals, and spreads
  (full game and halves). Pick two teams, choose a market, enter the
  posted line manually.
- **CS2** — Counter-Strike 2 research. Uses the HLTV Node.js bridge
  (see CS2 setup above) as the default source. Without the bridge
  installed, mock demo data is shown. GRID is an experimental future
  option (`CS2_PROVIDER=grid`).

To stop the app: press `Ctrl+C` in the terminal.

Next time you want to run it, just:

```bash
source .venv/bin/activate        # (or .venv\Scripts\Activate.ps1 on Windows)
streamlit run app.py
```

---

## How to use the dashboard

Everything you control is in the **left sidebar**:

1. **Player** — type a name, pick from the dropdown.
2. **Season** — defaults to the current season.
3. **Stat** — pick Points / Rebounds / PRA / etc.
4. **Prop line** — type the number the sportsbook posted.
5. **Projection weights** — how much to trust last 5 games vs last 10 vs
   season average. Defaults: 50% / 30% / 20%.
6. **Adjustments** — optional toggles:
   - **Matchup**: nudges the projection up/down based on how many
     points/rebounds/etc. the opponent team usually allows.
   - **Home/away**: uses the player's home-vs-road split for the stat.
   - **Minutes**: scales by projected minutes vs season average.
7. **Refresh data** — forces the app to re-fetch from the NBA stats
   endpoints, bypassing the local cache.

The main area has **five tabs**:

- **Overview** — summary cards and a one-sentence takeaway.
- **Game Logs** — filterable table of every game this season.
- **Splits** — L5/L10/L20 averages, hit rate table, home/away, W/L,
  rest-day, vs-opponent.
- **Trends** — line charts of the stat and minutes over time.
- **Projection Details** — every number used in the projection, plus a
  plain-English step-by-step explanation.

---

## How the projection works (in English)

The formula is deliberately simple:

```
base  = 0.50 × (average of last 5 games)
      + 0.30 × (average of last 10 games)
      + 0.20 × (season average)

final = base × matchup × home/away × minutes
```

Each adjustment is off by default and equals `1.0` (no effect) when off
or when we can't compute it. All factors are **clamped** so a tiny
sample can't produce wild 2x swings:

- Matchup clamped to `[0.85, 1.15]`
- Home/away clamped to `[0.80, 1.20]`
- Minutes clamped to `[0.70, 1.30]`

The **Projection Details** tab shows every number used. Nothing is
hidden.

---

## Troubleshooting

**"No module named streamlit" / "command not found: streamlit"**
You forgot to activate the virtual environment. Run
`source .venv/bin/activate` (or the Windows equivalent) and try again.

**`ImportError: numpy.core.multiarray failed to import`** (often right before `import pandas`)
Your shell is probably running Streamlit with a different Python than this project's virtualenv — commonly **Anaconda base**, where NumPy/Pandas wheels can get out of sync after mixed `conda install` / `pip install`. Run the app **only** after `source .venv/bin/activate`, then `pip install -r requirements.txt`, then `streamlit run app.py`. If you insist on conda instead, reinstall matching builds there (e.g. `conda install -y numpy pandas` in that env).

**nba_api calls time out or return nothing**
The free stats.nba.com endpoints are rate-limited and sometimes flaky.
The app retries twice automatically. If it still fails, wait 30 seconds
and hit the sidebar's **Refresh data** button.

**"No game-log data available for this player/season"**
The player has zero games logged for that season. Try a different
season, or a player who's actually playing.

**Opponent shows "last game's opponent" instead of tonight's**
The free `ScoreboardV2` endpoint only reliably reports games scheduled
for today. When nothing is scheduled, we fall back to the last-played
opponent so the matchup panel still has something to show.

**"Adjustment returned neutral (factor = 1.00)"**
Means the toggle is on but we didn't have enough data to compute it
(e.g. the season is too young, or the NBA defense endpoint failed).
Open the Projection Details tab for the specific reason.

**Want a clean slate?**
Delete `data/cache.sqlite` and reload the app.

---

## Known nba_api limitations

The free `nba_api` library wraps stats.nba.com, which has quirks:

- **Rate limiting.** Spamming refresh triggers 429s. The app retries,
  but heavy use in a short window will produce temporary failures.
- **ScoreboardV2 is today-only.** We can't reliably get tomorrow's
  schedule from the free endpoints, so "next opponent" can fall back to
  "last opponent" after the day's games finish.
- **Team-level defense only.** `LeagueDashTeamStats(MeasureType='Opponent')`
  gives *team* averages allowed. Positional defense ("opponent vs PGs")
  needs a paid or scraped source — that's a deliberate v1 tradeoff.
- **Empty early-season data.** In October, many players have fewer than
  5 games; the projection drops missing windows and renormalizes weights
  so it still returns a number, but the sample is small.
- **Preseason / All-Star games excluded** by our `Regular Season` filter.
- **Historical players** may have game logs that predate some modern
  columns (e.g. minutes as `MM:SS` vs float). The loader handles both.
- **CommonPlayerInfo** (used to get the player's current team) is the
  slowest endpoint and the most likely to time out; when it does, we
  show the opponent panel from the game log instead.
- **The static player list** ships with nba_api and is updated only when
  you `pip install -U nba_api`. A player who signed with the league this
  week might not appear until the next release.

None of these crash the app — every failure path logs a warning and
degrades to something readable.

---

## Project structure

```
Outlier/
  app.py                  # Streamlit entry point; sidebar Mode radio routes to
                          # Player Props, render_game_markets_page(), or render_cs2_page()
  config.py               # ALL constants: stats, colors, TTLs, API params, HLTV + GRID config
  requirements.txt
  .env.example            # template for all environment variable overrides
  data/                   # runtime caches (gitignored)
    cache.sqlite          # NBA SQLite cache (gamelogs, schedules, defense, etc.)
    cs2_cache/            # CS2 file-system JSON cache (GRID + HLTV, shared)
  database/
    cache_db.py           # key-value SQLite cache helpers
    schema.sql
  services/
    nba_client.py         # all nba_api calls: circuit breaker + rate limiter
    gamelog_service.py    # player game log + SQLite cache + stale fallback
    schedule_service.py   # next-opponent via ScoreboardV2
    defense_service.py    # team-level allowed stats
    rankings_service.py   # chip rank maps (pace, defRtg, allowed stats)
    filter_service.py     # PURE filter pipeline; no network
    partial_game_service.py  # PlayByPlayV3-derived Q1/H1 player stats (+ quarter_service shims)
    player_service.py     # player search (nba_api static list)
    suggestion_service.py # rule-based Suggested Filters
    teammate_service.py   # teammate game-log summary
    team_service.py       # team game log + SQLite cache
    matchup_service.py    # PairContext for Game Markets (H2H, form, schedule)
    linescore_service.py  # per-game 1H/2H line scores (permanent cache)
    game_market_projection_service.py  # PURE Game Markets projection engine
    cs2/
      hltv_bridge/        # Node.js gigobyte/hltv bridge (primary data source)
        package.json
        index.mjs
      hltv_provider.py    # Python wrapper around hltv_bridge (cache/throttle/CB)
      grid_provider.py    # GRID Open Access client — experimental future path
      mock_provider.py    # static demo data
      cs2_cache_service.py # shared file-system JSON cache
      models.py           # internal dataclasses (CS2Team, CS2Player, CS2MatchResult,
                          #   CS2PlayerStats, CS2TeamStats, CS2Tournament, CS2SourceStatus)
      cs2_capabilities.py # provider-aware feature registry (drives UI notices)
      cs2_data_provider.py # HLTV-first facade; normalises raw data → internal models
      cs2_match_service.py
      cs2_team_service.py
      cs2_player_service.py
      cs2_map_service.py
      cs2_projection_service.py
      pandascore_client.py   # optional supplement
      pandascore_provider.py # optional supplement
  models/
    stats.py              # add_derived_stats() — PRA/PA/RA + DD/TD
    projection.py         # project() → ProjectionResult
    metrics.py            # hit rates, stat card diffs
    hit_rate.py
    rolling.py
    splits.py
  ui/
    filter_state.py       # FilterState dataclass in st.session_state
    filter_modal.py       # Filters @st.dialog
    teammate_modal.py     # Filter-by-Teammate @st.dialog
    graph.py              # render_research_graph()
    layout.py             # top bar, key metrics row
    components.py         # shared HTML helpers (chips, badges)
    theme.py              # inject_theme(); CSS from config.COLORS
    game_markets_page.py  # render_game_markets_page()
    cs2_page.py           # render_cs2_page(); 6 tabs (HLTV-primary, GRID audit panel appears only when CS2_PROVIDER=grid)
    cs2_components.py     # CS2-specific UI helpers
  utils/
    formatting.py         # format_team_margin(), spread_interpretation(), etc.
```

---

## Future improvements

**NBA / Player Props**
- Positional defense (vs-PG, vs-C) via a free scraper with fallback.
- Monte Carlo simulation giving a full distribution of outcomes.
- CSV / Parquet export of any table.
- Injury flags and depth-chart context via a free news source.
- Multi-player compare view.
- Prop-line history tracker (log lines you've seen to track CLV).

**Game Markets**
- Parallelize `enrich_team_log_with_halves()` BoxScore fetches.
- Expose opponent-strength and explicit rest-day chips in the inline filter.
- Wire the upcoming matchup picker to today's ScoreboardV2 schedule.
- Empirically fit `HALF_SCORING_SHARE` / `HALF_SPREAD_SHARE` from cached line scores.

**CS2**
- Trend / sparkline charts for player kills / ADR / rating from the
  per-map history already normalised by the HLTV provider.
- Team-vs-team map-pool side-by-side diff in the Maps tab.
- Optional export of the filtered results table (CSV).
- Revisit GRID once its free Open Access tier includes Central Data
  GraphQL — the provider abstraction is preserved for that switch.
- Explore an alternative HLTV bridge or local snapshot source as
  insurance against gigobyte/hltv falling further behind upstream.

---

## License

Personal use.
