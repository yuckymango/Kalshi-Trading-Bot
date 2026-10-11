import math
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from functools import lru_cache

import nflreadpy as nfl
import pandas as pd

HOME_FIELD_POINTS = 2.0
POINT_STD = 13.5  # NFL single-game margin standard deviation
PLAYS_PER_GAME = 65.0
PRIOR_SEASON_GAMES = 8
HALFLIFE_GAMES = 6
SHRINK_GAMES = 4
MIN_P, MAX_P = 0.01, 0.99

# Heuristic weights for converting per-game stats to point-equivalents.
W_EPA, W_SUCCESS, W_MARGIN, W_WPA = 0.50, 0.15, 0.25, 0.10
SUCCESS_TO_POINTS = 60.0
WPA_TO_POINTS = 30.0

# Injury deductions in points. EDGE rushers are listed as DE/OLB in the injury report.
QB_OUT_POINTS = 4.5
SKILL_OUT_POINTS = 0.75
SKILL_POSITIONS = {"WR", "CB", "EDGE", "DE", "OLB"}
OUT_STATUSES = {"out", "doubtful"}

# Kalshi abbreviations -> nflreadpy abbreviations.
TEAM_ALIASES = {"JAC": "JAX", "LAR": "LA", "WSH": "WAS"}

TEAMS = {
    "ARI", "ATL", "BAL", "BUF", "CAR", "CHI", "CIN", "CLE", "DAL", "DEN", "DET", "GB", "HOU", "IND", "JAX", "KC",
    "LA", "LAC", "LV", "MIA", "MIN", "NE", "NO", "NYG", "NYJ", "PHI", "PIT", "SEA", "SF", "TB", "TEN", "WAS",
} | set(TEAM_ALIASES)

_TICKER_RE = re.compile(r"-(\d{2})([A-Z]{3})(\d{2})([A-Z]{4,8})-([A-Z]{2,4})$")


class UnsupportedMatchup(Exception):
    pass


def build_matchup(ticker: str) -> dict:
    """Parse e.g. KXNFLGAME-26SEP13BUFMIA-BUF (away team listed first; last segment is the YES team)."""
    if "NFL" not in ticker.upper():
        raise UnsupportedMatchup(f"No model for {ticker}")
    m = _TICKER_RE.search(ticker.upper())
    if not m:
        raise UnsupportedMatchup(f"Cannot parse matchup from {ticker}")
    yy, mon, dd, teams, yes_team = m.groups()
    splits = [(teams[:i], teams[i:]) for i in (2, 3, 4) if teams[:i] in TEAMS and teams[i:] in TEAMS]
    if not splits:
        raise UnsupportedMatchup(f"Cannot split teams in {ticker}")
    away, home = splits[0]
    if yes_team not in (away, home):
        raise UnsupportedMatchup(f"Cannot map YES team in {ticker}")
    game_date = datetime.strptime(f"{yy}{mon}{dd}", "%y%b%d")
    return {"league": "NFL", "date": game_date, "away": away, "home": home, "team": yes_team}


def _norm(team: str) -> str:
    return TEAM_ALIASES.get(team, team)


@lru_cache(maxsize=4)
def _team_games(season: int) -> pd.DataFrame:
    """One row per team-game with offense/defense EPA, success rate, WPA and point margin."""
    pbp = nfl.load_pbp([season]).select(
        ["game_id", "posteam", "season_type", "pass", "rush", "epa", "wpa", "success", "game_date"]
    ).to_pandas()
    plays = pbp[pbp["posteam"].notna() & ((pbp["pass"] == 1) | (pbp["rush"] == 1)) & pbp["epa"].notna()]
    off = (
        plays.groupby(["game_id", "posteam"])
        .agg(
            date=("game_date", "first"),
            season_type=("season_type", "first"),
            epa=("epa", "mean"),
            success=("success", "mean"),
            wpa=("wpa", "sum"),
        )
        .reset_index()
        .rename(columns={"posteam": "team"})
    )
    opp = off[["game_id", "team", "epa", "success", "wpa"]].rename(
        columns={"team": "opp", "epa": "def_epa", "success": "def_success", "wpa": "opp_wpa"}
    )
    g = off.merge(opp, on="game_id")
    g = g[g["team"] != g["opp"]].copy()

    sched = nfl.load_schedules([season]).to_pandas()[["game_id", "home_team", "away_team", "home_score", "away_score"]]
    g = g.merge(sched, on="game_id", how="left")
    is_home = g["team"] == g["home_team"]
    g["margin"] = (g["home_score"] - g["away_score"]).where(is_home, g["away_score"] - g["home_score"])
    g["net_epa"] = g["epa"] - g["def_epa"]
    g["net_success"] = g["success"] - g["def_success"]
    g["net_wpa"] = g["wpa"] - g["opp_wpa"]
    g["date"] = pd.to_datetime(g["date"])
    return g.sort_values("date").reset_index(drop=True)


@lru_cache(maxsize=2)
def _injuries(season: int) -> pd.DataFrame:
    try:
        return nfl.load_injuries([season]).to_pandas()
    except Exception:
        return pd.DataFrame()  # report not published or source unavailable


@lru_cache(maxsize=2)
def _schedule(season: int) -> pd.DataFrame:
    return nfl.load_schedules([season]).to_pandas()


@lru_cache(maxsize=2)
def _qb1_chart(season: int) -> pd.DataFrame:
    """Starting-QB rows (pos_rank 1) from every depth-chart snapshot."""
    try:
        d = nfl.load_depth_charts([season]).to_pandas()
    except Exception:
        return pd.DataFrame()
    d = d[(d["pos_abb"] == "QB") & (d["pos_rank"] == 1)][["dt", "team", "gsis_id"]]
    d["dt"] = pd.to_datetime(d["dt"], utc=True)
    return d


def _qb1_ids(team: str, season: int, date: datetime) -> set[str]:
    chart = _qb1_chart(season)
    if chart.empty:
        return set()
    chart = chart[chart["team"] == team]
    cutoff = pd.Timestamp(date.date(), tz="UTC") + pd.Timedelta(days=1)
    snap = chart[chart["dt"] <= cutoff]
    if snap.empty:
        return set()
    return set(snap[snap["dt"] == snap["dt"].max()]["gsis_id"])


def _game_week(home: str, away: str, date: datetime, season: int) -> int | None:
    s = _schedule(season)
    g = s[(s["home_team"] == home) & (s["away_team"] == away) & (pd.to_datetime(s["gameday"]) == pd.Timestamp(date.date()))]
    return int(g["week"].iloc[0]) if not g.empty else None


def injury_deduction(team: str, season: int, week: int | None, date: datetime) -> float:
    """Points to subtract from a team's rating for Out/Doubtful players that week; 0 if no report.

    The QB deduction applies only when the depth-chart starter (QB1) is out.
    """
    inj = _injuries(season)
    if week is None or inj.empty or not {"team", "week", "position", "report_status", "gsis_id"} <= set(inj.columns):
        return 0.0
    rows = inj[(inj["team"] == team) & (inj["week"] == week)]
    out = rows[rows["report_status"].str.lower().isin(OUT_STATUSES)]
    if out.empty:
        return 0.0
    positions = out["position"].str.upper()
    qb1_out = out[positions == "QB"]["gsis_id"].isin(_qb1_ids(team, season, date)).any()
    return (QB_OUT_POINTS if qb1_out else 0.0) + SKILL_OUT_POINTS * float(positions.isin(SKILL_POSITIONS).sum())


def team_rating(team: str, season: int, before: datetime) -> float:
    """Point-equivalent strength: blend of EPA, success rate, WPA and margin (recency-weighted, shrunk)."""
    cur = _team_games(season)
    cur = cur[(cur["team"] == team) & (cur["date"] < before)]
    prev = _team_games(season - 1)
    prev = prev[prev["team"] == team].tail(PRIOR_SEASON_GAMES)
    games = pd.concat([prev, cur], ignore_index=True).dropna(subset=["net_epa", "margin"])
    if games.empty:
        return 0.0

    def ewm(col: str) -> float:
        return float(games[col].ewm(halflife=HALFLIFE_GAMES).mean().iloc[-1])

    points = (
        W_EPA * ewm("net_epa") * PLAYS_PER_GAME
        + W_SUCCESS * ewm("net_success") * SUCCESS_TO_POINTS
        + W_WPA * ewm("net_wpa") * WPA_TO_POINTS
        + W_MARGIN * ewm("margin")
    )
    return points * len(games) / (len(games) + SHRINK_GAMES)


def calculate_true_probability(matchup_data: dict) -> float:
    """Win probability (0.01-0.99) of matchup_data['team'] from nflreadpy play-by-play and results."""
    if matchup_data.get("league") != "NFL":
        raise UnsupportedMatchup(f"No model for league {matchup_data.get('league')}")
    date = matchup_data["date"]
    season = date.year if date.month >= 3 else date.year - 1
    home, away = _norm(matchup_data["home"]), _norm(matchup_data["away"])
    week = _game_week(home, away, date, season)

    home_rating = team_rating(home, season, date) - injury_deduction(home, season, week, date)
    away_rating = team_rating(away, season, date) - injury_deduction(away, season, week, date)
    expected_home_margin = home_rating - away_rating + HOME_FIELD_POINTS
    p_home = 0.5 * (1 + math.erf(expected_home_margin / (POINT_STD * math.sqrt(2))))
    p = p_home if matchup_data["team"] == matchup_data["home"] else 1 - p_home
    return float(min(MAX_P, max(MIN_P, p)))


def kickoff_utc(matchup_data: dict) -> datetime | None:
    """Scheduled kickoff (UTC) from the NFL schedule, which lists gametime in US Eastern."""
    date = matchup_data["date"]
    season = date.year if date.month >= 3 else date.year - 1
    home, away = _norm(matchup_data["home"]), _norm(matchup_data["away"])
    s = _schedule(season)
    g = s[(s["home_team"] == home) & (s["away_team"] == away) & (pd.to_datetime(s["gameday"]) == pd.Timestamp(date.date()))]
    if g.empty or not isinstance(g["gametime"].iloc[0], str):
        return None
    local = datetime.fromisoformat(f"{g['gameday'].iloc[0]}T{g['gametime'].iloc[0]}").replace(tzinfo=ZoneInfo("America/New_York"))
    return local.astimezone(timezone.utc)
