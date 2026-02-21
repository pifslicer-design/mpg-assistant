"""MPG — export JSON à la demande depuis SQLite.

Réutilise les fonctions DB et bonus existantes, sans duplication.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from mpg_db import get_conn
from bonus_catalog import BONUS_CATALOG, CONSUMABLE_KEYS
from mpg_bonuses import compute_remaining_bonuses

SCHEMA_VERSION = 1


# ── Builders ────────────────────────────────────────────────────────────────

def build_league_export() -> dict:
    with get_conn() as conn:
        row = conn.execute("SELECT raw_json FROM league LIMIT 1").fetchone()
    if not row:
        return {}
    return json.loads(row["raw_json"])


def build_teams_export(division_id: str) -> list[dict]:
    with get_conn() as conn:
        teams = conn.execute(
            "SELECT id, name, user_id, budget, raw_json FROM teams WHERE division_id=?",
            (division_id,),
        ).fetchall()
        players_by_team: dict[str, list] = {}
        for t in teams:
            rows = conn.execute(
                "SELECT id, bid_date, price, status FROM players WHERE team_id=?",
                (t["id"],),
            ).fetchall()
            players_by_team[t["id"]] = [dict(r) for r in rows]

    result = []
    for t in teams:
        result.append({
            "id":       t["id"],
            "name":     t["name"],
            "user_id":  t["user_id"],
            "budget":   t["budget"],
            "players":  players_by_team.get(t["id"], []),
        })
    return result


def build_matches_export(
    division_id: str,
    gw_min: int | None = None,
    gw_max: int | None = None,
) -> list[dict]:
    with get_conn() as conn:
        # Index noms d'équipes
        team_names = {
            r["id"]: r["name"]
            for r in conn.execute("SELECT id, name FROM teams WHERE division_id=?", (division_id,)).fetchall()
        }

        filters = ["division_id=?"]
        params: list = [division_id]
        if gw_min is not None:
            filters.append("game_week >= ?")
            params.append(gw_min)
        if gw_max is not None:
            filters.append("game_week <= ?")
            params.append(gw_max)

        rows = conn.execute(
            f"SELECT * FROM matches WHERE {' AND '.join(filters)} ORDER BY game_week, id",
            params,
        ).fetchall()

    result = []
    for r in rows:
        home_bonuses = json.loads(r["home_bonuses"] or "{}")
        away_bonuses = json.loads(r["away_bonuses"] or "{}")
        result.append({
            "id":           r["id"],
            "game_week":    r["game_week"],
            "is_finalized": bool(r["is_finalized"]),
            "home": {
                "team_id":   r["home_team_id"],
                "team_name": team_names.get(r["home_team_id"], r["home_team_id"]),
                "score":     r["home_score"],
                "bonuses":   home_bonuses,
            },
            "away": {
                "team_id":   r["away_team_id"],
                "team_name": team_names.get(r["away_team_id"], r["away_team_id"]),
                "score":     r["away_score"],
                "bonuses":   away_bonuses,
            },
            "raw_json": json.loads(r["raw_json"] or "{}"),
        })
    return result


def build_bonus_catalog_export() -> list[dict]:
    return [
        {
            "api_key":       key,
            "ui_label":      v["ui_label"],
            "stock_default": v["stock_default"],
            "is_consumable": v["is_consumable"],
        }
        for key, v in BONUS_CATALOG.items()
    ]


def build_bonus_remaining_export(division_id: str) -> dict:
    with get_conn() as conn:
        team_names = {
            r["id"]: r["name"]
            for r in conn.execute(
                "SELECT id, name FROM teams WHERE division_id=?", (division_id,)
            ).fetchall()
        }

    remaining = compute_remaining_bonuses(division_id=division_id)

    result = {}
    for team_id, bonuses in remaining.items():
        result[team_id] = {
            "team_name": team_names.get(team_id, team_id),
            "bonuses": {
                api_key: {
                    "ui_label":  BONUS_CATALOG[api_key]["ui_label"],
                    "used":      info["used"],
                    "total":     info["total"],
                    "remaining": info["remaining"],
                }
                for api_key, info in bonuses.items()
                if api_key in CONSUMABLE_KEYS
            },
        }
    return result


# ── Assemblage ──────────────────────────────────────────────────────────────

SCOPES = ("league", "teams", "matches", "bonuses", "all")


def build_export(
    division_id: str,
    scope: str = "all",
    gw_min: int | None = None,
    gw_max: int | None = None,
) -> dict:
    include_all = scope == "all"

    payload: dict = {
        "meta": {
            "exported_at":    datetime.now(timezone.utc).isoformat(),
            "division_id":    division_id,
            "source":         "mpg.db",
            "schema_version": SCHEMA_VERSION,
            "scope":          scope,
            "gw_min":         gw_min,
            "gw_max":         gw_max,
        }
    }

    if include_all or scope == "league":
        payload["league"] = build_league_export()

    if include_all or scope == "teams":
        payload["teams"] = build_teams_export(division_id)

    if include_all or scope == "matches":
        payload["matches"] = build_matches_export(division_id, gw_min=gw_min, gw_max=gw_max)

    if include_all or scope == "bonuses":
        payload["bonus_catalog"]   = build_bonus_catalog_export()
        payload["bonus_remaining"] = build_bonus_remaining_export(division_id)

    return payload


# ── Écriture ────────────────────────────────────────────────────────────────

def write_export(data: dict, path: str, pretty: bool = False) -> None:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    indent = 2 if pretty else None
    out.write_text(json.dumps(data, ensure_ascii=False, indent=indent), encoding="utf-8")
    size_kb = out.stat().st_size / 1024
    matches_count = len(data.get("matches", []))
    print(f"[EXPORT] {out} — {size_kb:.1f} KB"
          + (f", {matches_count} matchs" if "matches" in data else ""))
