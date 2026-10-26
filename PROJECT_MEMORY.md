# PROJECT MEMORY
**Last verified:** 2026-04-22 (session 4) · **Run:** `streamlit run app.py`

---

## Project
**Outlier** — private, local-only, fully-free research dashboard. Top-level modes (sidebar radio in `main()`):
1. **NBA Player Props** — NBA player + market + manual prop line → projections + hit rates + filters + teammates (`app.py`).
2. **WNBA Player Props** — same UI flow via `ui/wnba_props_page.py`; stats.nba.com **LeagueID 10**; season strings like `2025` (not `2025-26`); filters stored in `filter_state_wnba` (`_props_league="wnba"`). Core services accept `league_id` / `league` kwargs (`gamelog_service`, `player_service`, `teammate_service`, `schedule_service`, `defense_service`, `rankings_service`).
3. **Game Markets** — matchup + market (full-game / 1H / 2H totals, team totals, spreads) + manual line → transparent projection + edge + lean + sample chart.
4. **CS2** — Counter-Strike 2 research (matches / players / teams / maps / tournaments). Provider priority: **GRID Open Access** (primary, requires `GRID_API_KEY`) → **HLTV** gigobyte/hltv Node.js bridge (fallback / enrichment) → **mock** demo data. Strict anti-ban + aggressive disk-cache architecture. PandaScore is an optional supplement only.

No paid APIs, no sportsbook data, no hosting.

### WNBA (League 10) notes
- **Config:** `league_config/wnba.py` (separate package so `config.py` is not shadowed) — `WNBA_STAT_COLUMNS`, seasons, no Q1/H1 markets yet; DD/TD not exposed.
- **Logos:** `utils/wnba_logos.py` (ESPN WNBA CDN).
- **Teammate modal:** `render_teammate_filter_modal(..., league_id=..., logo_url_fn=wnba_team_logo_url_png)`.
---

## Tech Stack
| | |
|---|---|
| UI | Streamlit ≥ 1.32, < 2.0 (`@st.dialog` requires ≥ 1.32) |
| Data | nba_api ≥ 1.4, < 2.0 → stats.nba.com |
| Processing | pandas ≥ 2.1, numpy ≥ 1.26 |
| Charts | plotly ≥ 5.18 |
| Persistent cache | SQLite at `data/cache.sqlite` via `database/cache_db.py` |
| In-process cache | `@st.cache_data` (Streamlit, TTL-based) |
| Language | Python ≥ 3.10 |

---

## Architecture

```
app.py                   entry point; top-of-sidebar mode radio; routes to Player Props
                         (legacy tabs), render_game_markets_page(), or render_cs2_page()
config.py                ALL constants: stats, markets, filter chips, colors, TTLs, API params
                         + GAME_MARKETS, GAME_MARKET_KIND/NEEDS_SIDE/DEFAULT_LINE,
                           HOME_COURT_ADV, HALF_SCORING_SHARE, HALF_SPREAD_SHARE,
                           GAME_MARKET_RECENT_N, CACHE_TTL_TEAM_GAMELOG, CACHE_TTL_LINESCORE

services/
  nba_client.py          ALL nba_api calls → call_endpoint(); circuit breaker + failure cache + rate limiter
  gamelog_service.py     PlayerGameLog; SQLite cache + stale fallback; _stale flag on DataFrame
  schedule_service.py    ScoreboardV2 next opponent; negative cache + stale fallback
  defense_service.py     LeagueDashTeamStats Opponent → matchup factor  [NO stale fallback yet]
  rankings_service.py    build_chip_rank_maps() for Opp Rankings tab    [NO stale fallback yet]
  filter_service.py      PURE filter pipeline; apply_all_filters() → FilterResult(filtered, excluded)
  quarter_service.py     Back-compat shims (`enrich_with_quarter_stats`, `quarter_stats_are_cached`).
  partial_game_service.py PlayByPlayV3-derived 1Q/1H PTS/AST/REB/3PM (period 1 vs 1–2); caches
                         `pbp_v3` + `box_roster_v3` per `game_id` (NBA Stats V3 endpoints via nba_client).
  player_service.py      player search via nba_api static list (no network)
  suggestion_service.py  rule-based Suggested Filters per market
  teammate_service.py    list_teammates(), fetch_teammate_summary(), build_teammate_table()
  team_service.py        [NEW] list_teams(), resolve_team(), fetch_team_game_log();
                         TeamGameLog + SQLite cache + stale fallback; normalizes
                         OPPONENT / IS_HOME / MARGIN / GAME_TOTAL / REST_DAYS
  matchup_service.py     [NEW] build_pair_context() → PairContext(teams, logs, recent,
                         advanced, home_team_id, stale); recent_form(), home_away_split(),
                         h2h_games/summary(), find_upcoming_matchup() (ScoreboardV2)
  linescore_service.py   [NEW] lazy BoxScoreSummaryV2.LineScore per game_id (permanent cache);
                         enrich_team_log_with_halves() adds PTS_1H/2H, OPP_PTS_1H/2H,
                         TOTAL_1H/2H, MARGIN_1H/2H; halves_are_cached()
  game_market_projection_service.py   [NEW] PURE (no network, no cache);
                         project(market, ctx, manual_line, side_team_id) → GameMarketProjection
                         (projection, edge, lean, factors, explanation, beta, sample_size);
                         per-market helpers: project_full_game_total / _team_total / _spread /
                         _half_total / _half_spread
  cs2/
    grid_provider.py     [NEW] GRID Open Access client (primary source).
                         GraphQL → Central Data API (allSeries, allTournaments, team,
                         player queries). REST → File Download API (end-state files).
                         Auth: x-api-key header (GRID_API_KEY env var).
                         Same anti-ban pattern as HLTV: disk cache-first (grid: prefix),
                         serialised lock, throttle (1 s default).
                         Audit helpers: audit_series() / audit_endstate() — expose raw
                         GRID JSON in a sidebar expander so field shapes can be verified
                         before writing normalisers.
                         ⚠️ End-state file format NOT yet audited; per-map player stats
                         from end-state are deferred until real responses are inspected.
    hltv_bridge/         Node.js package (gigobyte/hltv 3.5.0); run `npm install` once.
      package.json       declares hltv dependency + ESM module config
      index.mjs          stateless CLI: receives JSON {"method","args"}, returns JSON
    hltv_provider.py     Python wrapper around hltv_bridge; fallback / enrichment path.
                         Anti-ban layers: 1) disk cache-first, 2) serialised lock,
                         3) throttle (3 s default), 4) circuit breaker (3 fails/120 s →
                         300 s cooldown), 5) stale-cache fallback.
                         Exposes: get_matches / get_results / get_match /
                         get_player_by_name / get_player_stats / get_team /
                         get_team_by_name / get_team_stats / get_team_ranking
    mock_provider.py     Static demo data in HLTV response shape; used when all live
                         providers fail or CS2_USE_MOCK_FALLBACK=true.
    cs2_cache_service.py Shared file-system JSON cache in data/cs2_cache/; SHA1-keyed
                         .json + .meta.json pairs; supports TTL / stale / invalidate.
                         Cache keys prefixed grid: or hltv: to avoid collisions.
    models.py            Internal dataclasses: CS2Tournament [NEW], CS2Team, CS2Player,
                         CS2MapResult, CS2MatchResult, CS2PlayerMapEntry, CS2PlayerStats,
                         CS2TeamMapStat, CS2TeamStats, CS2SourceStatus [updated for GRID].
                         CS2SourceStatus now has grid_configured, grid_last_success_ts,
                         hltv_enabled, hltv_bridge_installed, hltv_cb_open/until fields.
    cs2_capabilities.py  Provider-aware capability registry. Two sections:
                         _GRID (conservative defaults, only confirmed GRID fields);
                         _HLTV_ENRICHMENT (ADR, team ranking, map win-rates, etc. —
                         active only when CS2_ENABLE_HLTV_ENRICHMENT=true + bridge OK).
                         supported(key), notice(key), source_for(key) are the public API.
    cs2_data_provider.py GRID-first facade. _active_provider() resolves: grid (if
                         GRID_API_KEY set) → hltv (if bridge installed) → mock.
                         GRID normalisers: _norm_match_from_grid (safe, wired),
                         _norm_tournament_from_grid, _norm_team_from_grid,
                         _norm_player_from_grid (all safe, wired).
                         ⚠️ End-state enrichment (_enrich_match_from_grid_endstate,
                         _norm_player_stats_from_grid_endstate) REMOVED until format
                         is audited. player_stats() with GRID falls through to HLTV
                         enrichment or returns None.
                         HLTV normalisers retained as-is (used for hltv provider path
                         and HLTV enrichment supplementation).
                         Backward-compat shims retained for smoke-test suite.
    cs2_match_service.py TeamForm (build_team_form), H2H, map-pool; legacy adapters kept
    cs2_team_service.py  Team profile + map win-rate aggregation (CS2TeamStats)
    cs2_player_service.py Player profile + stats; build_player_sample_dict() bridges
                         CS2PlayerStats → projection engine's expected dict shape
    cs2_map_service.py   Map-level aggregation derived from loaded player/team stats
    cs2_projection_service.py PURE CS2 projections (match winner / player kills /
                         player headshots)
    pandascore_client.py Optional supplement; PandaScore HTTP wrapper (throttle +
                         circuit breaker + SQLite cache); not required for core features
    pandascore_provider.py Optional supplement; endpoint wrappers for schedule data only

models/
  stats.py               add_derived_stats() → PRA/PA/RA/PR/Stocks + DD/TD (threshold-based)
  projection.py          project() → ProjectionResult; weighted L5/L10/Season × optional factors
  metrics.py             compute_hit_rate(), compute_graph_avg(), compute_stat_card_diffs()
  hit_rate.py            hit_rate(), hit_rates_over_windows()
  rolling.py             compute_rolling_summary(), rolling_series()
  splits.py              home_away_split(), win_loss_split(), rest_day_split()

ui/
  filter_state.py        FilterState dataclass → st.session_state["filter_state"]  (Player Props only)
  filter_modal.py        Filters @st.dialog: Season/Games + Suggested/Opp Rankings/Splits/Stats tabs
  teammate_modal.py      Filter-by-Teammate @st.dialog; on_pill_click() toggle logic
  graph.py               render_research_graph(); _drop_dnp() pure helper
  layout.py              render_top_bar(), render_key_metrics_row(), render_teammate_row()
  components.py          chip_html, rank_badge_html, action_pill, status_chip, headshot_html
  theme.py               inject_theme(); all CSS built from COLORS tokens in config.py
  game_markets_page.py   [NEW] render_game_markets_page(); self-contained page: matchup selector,
                         market + side + manual-line inputs, inline filter expander (season /
                         last-N / H/A / regular-vs-playoffs / B2B / H2H-only / pace range),
                         summary cards row, main bar chart (over/under green/red), 6 tabs
                         (Overview / Recent Games / Splits / H2H / Projection Details /
                         Half Analysis). Owns "Load half-level data (beta)" action.
  cs2_page.py            render_cs2_page(); 6 tabs: Overview / Matches / Players /
                         Teams / Maps / Tournaments [NEW].
                         Anti-ban lazy-load design: Overview auto-loads ONE endpoint
                         (recent results, 6-h cache); all other data behind explicit
                         user buttons. _SK_TOURNAMENTS [NEW] session-state key added.
                         GRID audit panel in sidebar (audit_series / audit_endstate
                         buttons) visible when GRID is configured — shows raw API JSON.
                         Players + Teams tabs show a clear capability notice when GRID
                         is primary and HLTV enrichment is disabled (no free-text search
                         in GRID Central Data API). _render_provider_header_caption()
                         uses dp.source_status().provider (resolved, not raw env var).
  cs2_components.py      CS2-only UI components: cs2_source_status_panel [updated],
                         cs2_stale_banner, cs2_cooldown_banner, cs2_mock_banner,
                         cs2_grid_no_key_banner [NEW], cs2_source_badge [NEW],
                         cs2_tournament_table [NEW], cs2_match_result_card,
                         cs2_player_stat_cards, cs2_player_map_history_table,
                         cs2_team_roster_card, cs2_team_map_pool_table

database/
  cache_db.py            get_cached(), set_cached(), get_cached_stale(), get_cache_age()
  schema.sql             cache_entries(namespace, cache_key, payload TEXT/JSON, created_at INT)
```

**SQLite cache namespaces:** `gamelog`, `pbp_v3`, `box_roster_v3`, `qbox` (legacy; cleared on refresh), `defense`, `rankings`, `schedule`, `teammates`, `players`, `team_gamelog` (6h), `linescore` (permanent), `cs2_pandascore` (optional PandaScore supplement)

**CS2 file-system cache:** `data/cs2_cache/` — JSON files managed by `cs2_cache_service.py`. Cache keys prefixed `grid:` (GRID) or `hltv:` (HLTV) to avoid collisions. TTLs: series list / results 6 h, upcoming 15 min, player/team stats 24 h, ranking 4 h, end-state files 30 days, tournament list 24 h. (Old path was `data/cs2_hltv_cache/` — both work if `CS2_CACHE_DIR` is set.)

---

## Coding Conventions

- **`config.py` is the single source of truth.** Never hardcode stat names, colors, TTLs, or API params elsewhere.
- **All nba_api calls go through `services/nba_client.call_endpoint()`** — never instantiate endpoints directly.
- **`filter_service.py` is pure** — no Streamlit, no network, no cache. Testable in isolation.
- **`add_derived_stats()`** is called in `app.py` after the raw log is fetched, not inside `gamelog_service`.
- **`FilterState`** (`st.session_state["filter_state"]`) is the only mutable UI state object. All filter modals read/write it directly.
- **`build_teammate_table()` has no `market_col` parameter.** It stores all stat averages in a `STAT_AVGS: dict` column. The modal picks the market column at display time. This was a deliberate fix to prevent 15 gamelog fetches per stat switch.
- **Stale cache data** is flagged by `_stale=True` column on the DataFrame; `render_research()` shows a warning banner.
- **DNP filtering** only applies to the bar chart (`_drop_dnp` in `graph.py`), not to metrics or the data layer.
- **CSS classes** are all prefixed `pp-` to avoid Streamlit collisions. All color tokens come from `config.COLORS`.
- **Beta chips** in the filter modal are UI-only — `filter_service` never reads them.
- **Game Markets mode is fully decoupled from `FilterState` / `filter_service`.** It uses its own lightweight inline filter (`_apply_sample_filters` in `ui/game_markets_page.py`). Do not route it through the player-prop filter pipeline.
- **`game_market_projection_service` is pure** (no Streamlit, no network, no cache). Every projection returns a `GameMarketProjection` with an explicit list of `Factor`s and a plain-English `explanation` — no black boxes.
- **Half-level line-score fetching is lazy and opt-in.** `linescore_service.enrich_team_log_with_halves()` is only invoked from the "Load half-level data (beta)" button; projections fall back to `HALF_SCORING_SHARE` / `HALF_SPREAD_SHARE` heuristics and mark themselves `beta=True` when real halves aren't cached.
- **All new Game Markets API calls go through `nba_client.call_endpoint()`** — same circuit breaker / failure cache / rate limiter as player-prop services.
- **Spread convention is one signed float** from the selected team's perspective: positive = that team wins by N, negative = loses by N. Posted spread uses the same sign (`-4.5` favourite, `+3.5` dog). UI collects it as sign-radio + positive magnitude but stores a signed float; the projection engine's API stays unchanged.
- **Sportsbook-style spread rendering is centralised in `utils.formatting`.** Use `format_team_margin(abbr, signed_value)` for "TOR -12.5" / "TOR +1.3" / "TOR PK" and `spread_interpretation(abbr, signed_value, market_window=...)` for plain-English cover text. Never rebuild these strings inline in `ui/` or `services/`.
- **CS2 mode is fully decoupled from NBA state/services.** All CS2 service code lives under `services/cs2/`; UI state keys use `_SK_*` session-state constants defined at the top of `cs2_page.py`; CS2 has no dependency on `FilterState`.
- **CS2 provider priority: GRID Open Access (primary) → HLTV gigobyte/hltv (fallback/enrichment) → mock.** Controlled by `CS2_PROVIDER` env var (default `grid`). `_active_provider()` in `cs2_data_provider.py` resolves the actual runtime provider. UI reads `dp.source_status().provider` — never `os.environ.get("CS2_PROVIDER")` directly, which can be stale if fallback occurred.
- **GRID anti-ban rules (apply alongside HLTV rules):**
  - `grid_provider._call()` is the only entry point for GRID data. Never call `_post_graphql()` / `_get_file()` directly.
  - Same lock-then-throttle pattern as HLTV: `_lock` (threading.Lock) serialises all calls; `_throttle()` enforces `GRID_THROTTLE_SECONDS` (default 1 s).
  - `audit_series()` / `audit_endstate()` bypass normalisation to return raw JSON — for development only; do not call in production render paths.
  - ⚠️ Do NOT add end-state normalisers until `audit_endstate()` confirms the field schema.
- **HLTV anti-ban rules (never relax without careful testing):**
  - `hltv_provider._call()` is the only entry point to the Node.js bridge. Never call `_run_bridge()` directly.
  - `_lock` (threading.Lock) serialises all subprocess calls — one at a time, no exceptions.
  - `_throttle()` sleeps `CS2_HLTV_THROTTLE_SECONDS` (default 3 s) between live requests. Only called inside `with _lock:`.
  - Circuit breaker: 3 failures within 120 s → all requests paused for 300 s; stale cache served automatically.
  - Disk cache is checked *before* acquiring the lock. Lock is re-checked under lock (double-check pattern).
  - `cs2_page.py` auto-loads at most ONE endpoint per session (recent results, Overview tab). All others are behind explicit user buttons.
  - `st.session_state` keys (`_SK_RESULTS`, `_SK_UPCOMING`, `_SK_PLAYER`, `_SK_P_STATS`, `_SK_TEAM`, `_SK_T_STATS`, `_SK_T_RESULTS`, `_SK_TOURNAMENTS`) prevent re-fetching on Streamlit rerenders.
  - `_clear_session()` + `dp.reset_provider()` + `st.cache_data.clear()` are called together by the sidebar Refresh button.
- **CS2 exceptions are typed:** HLTV: `HLTVError` → `HLTVCloudflare` / `HLTVCooldown` / `HLTVNotReady`. GRID: `GRIDError` → `GRIDAuthError` (401/403) / `GRIDNotConfigured` (no API key). `_handle_exc()` in `cs2_page.py` maps each to a distinct user-facing banner.
- **Stale payload convention for CS2:** stale dict payloads carry `_stale=True`; stale list payloads mark each dict element with `_stale=True` so list views can show `cs2_stale_banner()`. Dataclasses expose `._stale` field.
- **`cs2_capabilities.py` is the single source of truth for feature support.** Drives `_GRID` (safe, confirmed) and `_HLTV_ENRICHMENT` (active only when `CS2_ENABLE_HLTV_ENRICHMENT=true` and bridge OK) sections. Unsupported features must render a capability notice — never empty widgets, NaN cards, or vague "beta" badges.
- **`cs2_data_provider.source_status()` uses `hltv_st = _hltv.get_status()`** — never shadow `st` (Streamlit) inside `cs2_data_provider.py`.
- **`cs2_cache_service` is separate from SQLite.** CS2 payloads live in `data/cs2_cache/` as JSON files; the old `cs2_pandascore` SQLite namespace is only used by the optional PandaScore supplement.
- **Free-text player/team search is NOT available from GRID Central Data API.** When GRID is primary and HLTV enrichment is off, the Players and Teams tabs show an informational banner explaining the limitation; search inputs are hidden. Enable `CS2_ENABLE_HLTV_ENRICHMENT=true` or switch to `CS2_PROVIDER=hltv` to restore search.
- **CS2 Tournaments tab is wired but schema-pending.** `_render_tournaments()` calls `dp.tournaments()`, which returns `CS2Tournament` objects from GRID `allTournaments`. The tab clearly states "schema audit pending" — do not add derived statistics until format is confirmed via the audit panel.

---

## Key Decisions

| Decision | Why |
|---|---|
| Free/local only; no paid APIs | Hard constraint |
| Pre Season excluded from "ALL" mode by default | Rarely useful; saves 1–3 cold API calls |
| `market_col` removed from `build_teammate_table()` | Prevents Streamlit cache bust on every stat switch |
| Stale SQLite data returned on fetch failure + `_stale` flag | Stale > empty |
| Circuit breaker: 3 failures/60s → pause all live requests 120s | Stops timeout storm when server is down |
| Per-endpoint failure cache: 90s cooldown | Stops Streamlit reruns from re-hammering failed endpoints |
| `NBA_API_TIMEOUT=12s`, `NBA_API_MAX_RETRIES=1` | Max ~26s block per failure (was 90s with 30s/3 retries) |
| Rate limiter: 0.35s minimum between API calls | Prevents burst-sending on teammate table cold load |
| Quarter/H1 stats lazy-loaded only when user selects those markets | Up to 164 BoxScore calls; permanent cache after first load |
| Hit rate: strict `>` for half-lines, `>=` for integer lines | Matches sportsbook convention |
| Projection factors clamped: matchup ±15%, home_away ±20%, minutes ±30% | Prevents outlier-data distortion |
| Both `@st.dialog` modals declared inside `main()` | Required by Streamlit's component tree semantics |
| Game Markets added as a **new top-level mode**, not a replacement | Hard requirement: never break player-prop workflow |
| Game-market projections are explicit weighted factors, not a learned model | User must see *why* (plain-English explanation panel) |
| Pace factor in totals clamped to [0.90, 1.10] | Avoid extreme-pace outliers distorting totals |
| Home-court advantage = 2.5 points (`config.HOME_COURT_ADV`) | Standard NBA assumption; one constant, easy to retune |
| Half markets use real `BoxScoreSummaryV2` line scores when loaded, else 48/52 (total) & 45/55 (spread) heuristics, marked `beta=True` | BoxScore is 1 call per game; heuristic keeps UI usable before load |
| `CACHE_TTL_LINESCORE = 1 year` (effectively permanent) | Final boxscores never change |
| `CACHE_TTL_TEAM_GAMELOG = 6 hours` | Matches player gamelog TTL |
| CS2 added as a **third top-level mode**, never nested under NBA | Keeps product areas independent; avoids coupling regressions |
| CS2 primary source = **GRID Open Access**; HLTV = fallback/enrichment; PandaScore = optional supplement | GRID is a structured API (no scraping risk); HLTV retained for enrichment (ADR, rankings, search) |
| GRID was adopted audit-first: speculative end-state normalisers removed; `audit_series()` / `audit_endstate()` added | Never write normalisers for a format you haven't inspected — prevents silent data corruption |
| `_active_provider()` resolves provider at runtime; UI reads `dp.source_status()` not `os.environ` | Env var says "grid" but GRID may fall back to HLTV if no API key — UI must reflect reality |
| Node.js bridge (`hltv_bridge/`) used to call gigobyte/hltv from Python | Library is Node.js-only; subprocess bridge is the cleanest isolation |
| CS2 data cached to disk (JSON files, `data/cs2_cache/`) not SQLite | File-system cache avoids SQLite serialisation overhead; shared between GRID and HLTV |
| CS2 Overview auto-loads exactly one endpoint; all others lazy | Minimises API requests per session; Cloudflare / rate-limit risk proportional to volume |
| `st.session_state` (not `@st.cache_data`) guards CS2 fetches | `@st.cache_data` can cache failure states; session_state is cleared explicitly on Refresh |
| CS2 unsupported features rendered via `cs2_capabilities.py` notice | Honest, intentional UI — never empty widgets or NaN projections for missing data |
| Players/Teams tabs in GRID-primary mode show informational banners instead of broken search | GRID Central Data API has no free-text search; silent failure is worse than explicit notice |

---

## Constraints
- No paid APIs · no hosting · no sportsbook data · local `streamlit run app.py` only
- Streamlit ≥ 1.32 required (`@st.dialog`, `st.columns(vertical_alignment=)`)
- Python ≥ 3.10 (`X | Y` union types used throughout)
- `data/cache.sqlite` path is hardcoded in `config.DB_PATH`; directory auto-created
- CS2 GRID provider requires `GRID_API_KEY` env var or Streamlit secret; without it, falls back to HLTV or mock automatically
- CS2 HLTV bridge requires **Node.js ≥ 16** and a one-time `cd services/cs2/hltv_bridge && npm install` (still needed for enrichment / fallback)
- CS2 PandaScore token (`PANDASCORE_TOKEN`) is **optional**; app is fully functional without it
- Cloudflare may block HLTV bridge requests from data-center / shared IPs; home/residential IPs work reliably

---

## Do Not Change
| What | Why |
|---|---|
| `filter_service.py` pipeline logic | Tested, pure, stable |
| `projection.py` formula and clamp values | Deliberate calibration |
| `FilterState` field names | `from_dict()` deserialization breaks on rename |
| `database/schema.sql` | Schema change requires cache migration |
| `on_pill_click()` in `teammate_modal.py` | Toggle semantics unit-tested |
| `_drop_dnp()` in `graph.py` | Pure helper, unit-tested |
| `config.COLORS` key names | Used as CSS template variables in `theme.py` |
| `STAT_COLUMNS` values (`"PTS"`, `"FG3M"`, etc.) | Used as DataFrame column keys throughout pipeline |
| `GameMarketProjection` / `Factor` / `PairContext` dataclass field names | UI reads them directly; rename breaks `game_markets_page.py` |
| `GAME_MARKETS` tuple ordering | Drives UI dropdown order (MVP priority: Full Total → Team Total → Full Spread → halves) |
| Signed-margin convention for spreads (`manual_line`, `GameMarketProjection.projection`) | UI + service both depend on it; flipping the sign silently breaks cover logic and every explanation string |
| `format_team_margin` / `spread_interpretation` signatures in `utils/formatting.py` | Called from UI (cards, chart annotation, caption) **and** projection explanations; locked by `TestSpreadFormatting` |
| `_APP_MODES` includes `"CS2"` as a top-level peer mode | Navigation contract; CS2 must never be nested under NBA menus |
| `services/cs2/*` module boundaries | Keeps CS2 and NBA logic isolated and maintainable |
| `hltv_provider._call()` anti-ban layers (lock order, throttle, CB, stale fallback) | Relaxing any layer risks Cloudflare bans or stale-state hiding |
| `grid_provider._call()` lock-then-throttle pattern | Same rationale as HLTV; GRID rate limits real too |
| `_SK_*` session-state keys in `cs2_page.py` (including `_SK_TOURNAMENTS`) | `_clear_session()` and the UI both enumerate them; a rename silently breaks Refresh |
| `CS2SourceStatus` field names (`grid_configured`, `hltv_enabled`, `hltv_bridge_installed`, `hltv_cb_open`, `provider_label`, etc.) | UI reads these directly in `cs2_source_status_panel` and `_render_provider_header_caption` |
| `CS2MatchResult` / `CS2PlayerStats` / `CS2TeamStats` / `CS2Tournament` dataclass field names | UI and services read fields directly; rename breaks rendering |
| `_active_provider()` return values: `"grid"`, `"hltv"`, `"mock"` | `cs2_capabilities.py` and UI checks compare against these literals |

---

## Known Issues / Risks

| Issue | Severity |
|---|---|
| stats.nba.com unreliable; all mitigation is defensive | High / external |
| `@st.cache_data` caches empty DataFrame on NBA failure → stale for up to 6h | Medium — Refresh button resets circuit breaker |
| Teammate table cold-start: ~15 serial gamelog fetches | Medium — circuit breaker limits cascade; SQLite-cached after first success |
| Quarter/H1: up to 164 BoxScore calls; partial failure mid-batch not cached | Medium |
| `GAME_IDS` sets in teammate_summary may not survive `@st.cache_data` serialization | **Uncertain** — Streamlit likely pickles correctly but untested |
| `defense_service` + `rankings_service` have no stale fallback | Low — planned |
| `FilterState.teammate_availability` lost on page reload (session-only) | Low / by design |
| Play Types + Shooting Zones filter chips are beta no-ops | Low — LeagueDashTeamShotLocations returns 403 intermittently |
| `win_loss` field exists in `FilterState` but no UI chip exposes it in the filter modal | Low — oversight |
| Half-market projections without loaded line scores use 48/52 & 45/55 heuristics (`beta=True`) | Medium — acceptable fallback; always labeled in UI |
| `enrich_team_log_with_halves()` cold load = 1 BoxScoreSummaryV2 call per recent game (≤10 by default) | Medium — rate-limited + circuit-breaker guarded; permanent cache after first success |
| `find_upcoming_matchup()` uses ScoreboardV2 for a single date; non-today dates may miss timezone edge cases | **Uncertain** — date handling not stress-tested |
| Game Markets page doesn't yet expose all filters spec'd (opponent-strength chip, B2B/rest beyond checkbox) | Low — MVP scope trade-off |
| GRID end-state file format not yet audited | **High** — per-map player stats from end-state files are deferred until `audit_endstate()` confirms field shapes. Normaliser stubs removed to prevent silent bad data. |
| GRID CS2 `titleId=4` is assumed, not confirmed | Medium — if wrong, all GRID queries return empty results silently |
| GRID Central Data API has no free-text player/team search | Medium (by design) — Players/Teams tabs show a notice; full functionality requires HLTV enrichment |
| Cloudflare blocks HLTV bridge requests from data-center / CI IPs | High (known) — app degrades gracefully to stale cache; home IP works |
| gigobyte/hltv library not actively maintained; HLTV schema changes may break parsing silently | Medium — monitor after HLTV site updates |
| HLTV getPlayerStats = 3 HTTP requests; getTeamStats = 4-5; Cloudflare risk scales with request volume | Medium — mitigated by lazy loading + 24-h disk cache |
| KAST%, side splits, pistol rounds, round totals not available in gigobyte/hltv schema | Low — documented in cs2_capabilities.py; rendered as notices, never fabricated |
| Player sample currently uses player's current team recent matches as proxy | Medium — practical shortcut; can miss transfers/stand-ins |

---

## Open Questions *(uncertain — needs confirmation)*
- Is the `season_selection` in FilterState (e.g. "25/26") intended as a *separate* control from the sidebar season picker ("2025-26")? Inferred yes: filter modal season overrides the sidebar scope.
- Should Pre Season be a UI toggle rather than a permanent default-off?
- Should `FilterState` persist to disk (JSON) across page reloads?
- Are `HALF_SCORING_SHARE` (0.48/0.52) and `HALF_SPREAD_SHARE` (0.45/0.55) the right league-wide priors, or should they be tuned empirically off loaded line scores?
- Should Game Markets get its own persistent state object (analogous to `FilterState`) once inline filters expand?
- **What is GRID's actual CS2 `titleId`?** Assumed `4` based on docs. If wrong, all GRID queries return empty. **Verify via `audit_series()` panel.**
- **What fields does the GRID end-state file contain?** Unknown schema — run `audit_endstate()` on a real series ID before writing normalisers.
- Should `CS2_ENABLE_HLTV_ENRICHMENT` default to `true` once GRID is fully verified, providing seamless ADR / ranking data without user configuration?
- For CS2, should player history be tied to historical rosters instead of current team proxy? **Uncertain**
- Is gigobyte/hltv still the best Node.js HLTV wrapper, or has a maintained successor emerged? **Needs periodic check**
- Should the HLTV bridge be replaced with direct Python scraping (e.g. Playwright) if Cloudflare blocks become persistent? **Uncertain — current bridge works from residential IPs**

---

## Next Priorities
1. Add stale fallback to `defense_service.py` + `rankings_service.py`
2. Parallelize teammate table load with `ThreadPoolExecutor` + semaphore (target: ~10s cold vs ~45s today)
3. Add `win_loss` chip to the Splits tab in `filter_modal.py`
4. Add smoke tests for stale-fallback code path (`TestGamelogStaleFallback`)
5. Partial-batch caching for quarter/H1 stats if BoxScore call fails mid-series
6. Optionally persist `FilterState` across page reloads
7. Game Markets: parallelize `enrich_team_log_with_halves()` BoxScore fetches (bounded ThreadPool + semaphore, same pattern as planned teammate parallelization)
8. Game Markets: expose opponent-strength and explicit rest-day chips in the inline filter expander
9. Game Markets: wire a real "upcoming matchup" picker using `find_upcoming_matchup()` output in the header
10. Empirically fit `HALF_SCORING_SHARE` / `HALF_SPREAD_SHARE` from cached line scores once enough games are loaded
11. **CS2 [URGENT]: Audit GRID end-state file format** — use the sidebar "GRID Response Audit" panel (`audit_endstate(series_id)`) on a real CS2 series, confirm field names, then implement `_enrich_match_from_grid_endstate()` and `_norm_player_stats_from_grid_endstate()` in `cs2_data_provider.py`
12. **CS2: Confirm GRID CS2 `titleId`** — run `audit_series()` and verify returned teams/matches are CS2; adjust `GRID_CS2_TITLE_ID` in config if needed
13. **CS2: Wire per-map player stats** — once end-state format is confirmed, add map-level player stats to match detail and player profile pages
14. CS2: Enable and test `CS2_ENABLE_HLTV_ENRICHMENT=true` to restore player search, ADR, and team ranking via HLTV supplement
15. CS2: Add event/tournament/date filters once GRID endpoint quota impact is understood
16. CS2: Add pagination strategy for deeper player/team result samples while respecting anti-ban limits
17. CS2: Instrument Cloudflare hit-rate over time; consider Playwright fallback if gigobyte/hltv degrades

---

## Changelog
**2026-04-22 — CS2 GRID Open Access migration + cleanup (session 4)**
- **Architecture:** GRID Open Access promoted to CS2 primary source. HLTV demoted to fallback/enrichment path. New `services/cs2/grid_provider.py` implements GraphQL (Central Data API) + REST (File Download API) client with the same lock→throttle→cache anti-ban pattern.
- **Audit-first approach:** Speculative end-state normalisers were written and then removed after user feedback ("audit actual GRID responses first"). `audit_series()` and `audit_endstate()` added to `grid_provider.py`; a "GRID Response Audit" sidebar panel added to `cs2_page.py` so raw API JSON can be inspected before writing normalisers. End-state format remains unaudited.
- **Models updated:** `CS2Tournament` dataclass added. `CS2SourceStatus` refactored to track `grid_configured`, `grid_last_success_ts`, `hltv_enabled`, `hltv_bridge_installed`, `hltv_cb_open/until`, `provider_label`.
- **Capabilities updated:** `cs2_capabilities.py` split into `_GRID` (conservative, confirmed) and `_HLTV_ENRICHMENT` (active only when enabled + bridge installed). `_GRID["adr"]` = False; `_HLTV_ENRICHMENT["adr"]` = True.
- **Provider routing:** `_active_provider()` in `cs2_data_provider.py` resolves runtime provider (grid → hltv → mock). UI reads `dp.source_status().provider` — not raw env var — to avoid showing wrong provider after fallback.
- **UX for GRID limitations:** Players + Teams tabs now show clear informational banners when GRID is primary without HLTV enrichment (no free-text search in GRID API); search inputs are hidden. Stats loading failure messages in GRID-only mode explain the limitation explicitly.
- **Cleanup fixes:** removed unused `import json` (grid_provider.py) and `import time` (cs2_data_provider.py); fixed `st.sidebar.expander()` double-nesting bug in `_render_grid_audit_panel()`; replaced all hardcoded "HLTV" labels in buttons, spinners, and captions with provider-agnostic text or dynamic `stats._source` references.
- **UI additions:** `cs2_grid_no_key_banner`, `cs2_source_badge`, `cs2_tournament_table` components; new "Tournaments" tab (`_render_tournaments()`); `_SK_TOURNAMENTS` session-state key.
- **Config:** `CS2_PROVIDER` default changed from `hltv` → `grid`. New constants: `GRID_API_KEY`, `GRID_BASE_URL`, `GRID_CS2_TITLE_ID`, `GRID_THROTTLE_SECONDS`, `GRID_TIMEOUT_SECONDS`, `GRID_MAX_RETRIES`, `CS2_ENABLE_HLTV_ENRICHMENT`. `CS2_CACHE_DIR` default changed from `cs2_hltv_cache` → `cs2_cache`.
- **Smoke tests:** `TestCS2Capabilities.test_supported_known_false_key` updated (`assertFalse(supported("adr"))` under GRID-primary without enrichment). 168/168 tests pass.
- **Memory updated:** project description, architecture block (grid_provider added, cache path updated, ui components updated), coding conventions (GRID anti-ban rules, provider-routing rule, search-limitation rule), key decisions (GRID migration decisions), constraints (GRID_API_KEY), Do Not Change (new `_SK_TOURNAMENTS`, `CS2SourceStatus` fields, `_active_provider()` return values), known issues (GRID-specific risks), open questions (titleId, end-state format), next priorities (GRID audit urgent items promoted to top).

**2026-04-22 — CS2 HLTV integration + import/wiring cleanup (session 3)**
- **Architecture:** CS2 primary source switched from PandaScore to HLTV (gigobyte/hltv, Node.js bridge). Entire `services/cs2/` rewritten: `hltv_bridge/`, `hltv_provider.py`, `mock_provider.py`, `cs2_cache_service.py`, `models.py`, `cs2_capabilities.py`, `cs2_data_provider.py` are all new or rewritten. `pandascore_client` / `pandascore_provider` retained as optional supplement.
- **Anti-ban architecture recorded:** serialised lock, throttle (3 s), circuit breaker (3/120 s → 300 s cooldown), disk-cache-first, stale-cache fallback, Cloudflare exception type. One auto-loaded endpoint per session (getResults); all others lazy behind buttons. `st.session_state` guards (not `@st.cache_data`) prevent re-fetching on rerenders.
- **Cleanup fixes applied in this session:**
  - `hltv_provider.py`: removed redundant `_last_call_ts_lock` (protected by `_lock` already) and duplicate `from typing import Any` at end of file.
  - `cs2_page.py`: removed unused `hltv_provider as _hltv` import and unused `cs2_cloudflare_notice` import (Cloudflare errors handled inline in `_handle_exc`).
  - `cs2_data_provider.py`: renamed `st = _hltv.get_status()` → `hltv_st` (avoids `st`/Streamlit collision); removed dead `from cs2_cache_service import cache_stats` import in non-mock `source_status()` path.
  - `services/cs2/__init__.py`: docstring and `__all__` updated to describe HLTV architecture; PandaScore moved to "optional supplement" comment.
- **Smoke tests:** 168/168 pass after all changes. Zero linter errors.
- **Constraints updated:** Node.js ≥ 16 required; PandaScore token now optional.
- **Memory updated:** project description, architecture block, coding conventions, key decisions, constraints, Do Not Change, Known Issues, Open Questions, Next Priorities — all CS2 sections now reflect HLTV-first design.

**2026-04-23 — CS2 circuit-breaker / cache-failure bug fix**
- **Root cause confirmed:** `_cached_upcoming` and `_cached_past` were catching `PandaScoreError` and returning `[]`; `@st.cache_data` then cached that empty list for 10 min, hiding recovery long after the circuit breaker auto-reset at 120 s. The PandaScore API itself (all 8 endpoints) was returning 200 OK throughout — the failure was purely in the Streamlit caching layer.
- **Fix:** Removed `except PandaScoreError: return []` from both match-list wrappers. Errors propagate; each render call site (`_render_overview`, `_render_matches`, `_render_maps`) catches locally and shows "Click **Refresh CS2 data** to retry." The sidebar Refresh button continues to manually reset the CB + clear caches.
- **Coding conventions:** Added rule that `_cached_upcoming`/`_cached_past` must not swallow `PandaScoreError`; added `cs2_capabilities.py` as single source of truth for free-tier feature support.
- **Key decisions:** Added two entries for the error-propagation choice and the capability-notice pattern.

**2026-04-23 — CS2 integration refinement (stability + coherence)**
- Updated project scope from NBA-only/two modes to three top-level modes including CS2.
- Architecture now records `services/cs2/*`, `ui/cs2_page.py`, `ui/cs2_filters.py`, and `ui/cs2_components.py`; cache namespaces now include `cs2_pandascore`.
- Coding conventions/Do Not Change now capture CS2 isolation rules, PandaScore-only MVP policy, stale-marker conventions for list/dict payloads, and 401/403 auth short-circuit behavior.
- Constraints now include PandaScore token requirement for CS2.
- Known risks/open questions/next priorities now include CS2 free-tier metric coverage limits, current-team player sampling trade-off, and enrichment/pagination follow-ups.

**2026-04-23 — Spread UX refactor (Game Markets)**
- Coding conventions: recorded the signed-margin convention for spreads and that sportsbook rendering is centralised in `utils.formatting.format_team_margin` / `spread_interpretation` — never rebuilt inline in UI or service code.
- Do Not Change: added the signed-margin convention and the two helper signatures (locked by `TestSpreadFormatting`, 9 tests).
- Nothing removed; player-props, totals, and team-total behaviour untouched.

**2026-04-23 — Game Markets mode added**
- Project description: now documents two top-level modes (Player Props, Game Markets) routed by a sidebar radio in `app.py`.
- Architecture: added `services/team_service.py`, `services/matchup_service.py`, `services/linescore_service.py`, `services/game_market_projection_service.py`, `ui/game_markets_page.py`; annotated `config.py` with new constants; added `team_gamelog` (6h) and `linescore` (permanent) cache namespaces.
- Coding conventions: documented Game Markets' independence from `FilterState` / `filter_service`, purity of the new projection service, lazy/opt-in half-level fetching with beta fallback, and mandatory routing through `nba_client`.
- Key decisions: recorded new-mode-not-replacement rule, pace-factor clamp [0.90, 1.10], `HOME_COURT_ADV=2.5`, half-scoring (0.48/0.52) and half-spread (0.45/0.55) heuristics, `CACHE_TTL_LINESCORE=1y`, `CACHE_TTL_TEAM_GAMELOG=6h`.
- Do Not Change: added `GameMarketProjection`/`Factor`/`PairContext` dataclass fields and `GAME_MARKETS` tuple ordering.
- Known issues: added beta-heuristic usage, BoxScore cold-load cost, upcoming-matchup date-edge uncertainty, and partial filter coverage in the Game Markets page.
- Open questions: added tuning of half-scoring priors and whether Game Markets needs a persistent state object.
- Next priorities: appended four Game-Markets follow-ups (items 7–10).
