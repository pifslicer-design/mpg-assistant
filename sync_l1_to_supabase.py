"""sync_l1_to_supabase.py — Sync données L1 MPG → Supabase PostgreSQL.

Usage:
    python sync_l1_to_supabase.py                      # incrémental
    python sync_l1_to_supabase.py --full               # backfill 2024+2025
    python sync_l1_to_supabase.py --season 2024 --gw 30 34
"""

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv()

# ── Config ───────────────────────────────────────────────────────────────────
MPG_BASE = "https://api.mpg.football"
DB_PATH = Path(__file__).parent / "mpg.db"
MANIFEST_DB = Path(__file__).parent / "sync_manifest.db"
BATCH_SIZE = 500
L1_SEASONS = [2024, 2025]
L1_TOTAL_GW = 34

# ultraPosition → position simple
_ULTRA_MAP = {10: "G", 20: "D", 21: "D", 30: "M", 31: "M", 40: "A"}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _env(key: str) -> str:
    v = os.getenv(key)
    if not v:
        raise ValueError(f"Manquant dans .env : {key}")
    return v


def ultra_to_pos(ultra: int) -> str:
    if ultra in _ULTRA_MAP:
        return _ULTRA_MAP[ultra]
    if 10 <= ultra < 20:
        return "G"
    if 20 <= ultra < 30:
        return "D"
    if 30 <= ultra < 40:
        return "M"
    return "A"


# ── Clients ───────────────────────────────────────────────────────────────────

def build_mpg_client() -> httpx.Client:
    """Client MPG authentifié (réutilise le pattern de build_client())."""
    token = _env("MPG_TOKEN")
    return httpx.Client(
        base_url=MPG_BASE,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        timeout=20.0,
    )


def build_supa_client() -> httpx.Client:
    """Client Supabase REST avec service_role key."""
    url = _env("SUPABASE_URL").rstrip("/")
    key = _env("SUPABASE_SERVICE_KEY")
    return httpx.Client(
        base_url=f"{url}/rest/v1",
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates",
        },
        timeout=30.0,
    )


# ── Manifest local (SQLite) ───────────────────────────────────────────────────

def _manifest_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(MANIFEST_DB)
    conn.execute("CREATE TABLE IF NOT EXISTS manifest (key TEXT PRIMARY KEY, value TEXT)")
    conn.commit()
    return conn


def get_manifest(key: str) -> str | None:
    with _manifest_conn() as conn:
        row = conn.execute("SELECT value FROM manifest WHERE key=?", (key,)).fetchone()
        return row[0] if row else None


def set_manifest(key: str, value: str) -> None:
    with _manifest_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO manifest (key, value) VALUES (?,?)", (key, value)
        )


# ── Supabase UPSERT par lots ──────────────────────────────────────────────────

def supa_upsert(supa: httpx.Client, table: str, rows: list[dict]) -> int:
    """UPSERT par lots de BATCH_SIZE. Retourne le nb total de rows envoyées."""
    if not rows:
        return 0
    total = 0
    for i in range(0, len(rows), BATCH_SIZE):
        batch = rows[i: i + BATCH_SIZE]
        resp = supa.post(f"/{table}", content=json.dumps(batch))
        if resp.status_code not in (200, 201):
            print(f"  [ERR] {table} — {resp.status_code}: {resp.text[:400]}")
            resp.raise_for_status()
        total += len(batch)
    return total


# ── Étape 1+2 : Pool L1 + next matches ───────────────────────────────────────

def sync_pool(mpg: httpx.Client, supa: httpx.Client) -> None:
    print("[1/2] Fetching championship pool L1...")
    resp = mpg.get("/championship-players-pool/1")
    resp.raise_for_status()
    data = resp.json()

    # L'API retourne {"poolPlayers": [...]} ou {"championshipPlayersPool": {...}} ou liste
    raw_pool = (
        data.get("poolPlayers")
        or data.get("championshipPlayersPool")
        or data.get("players")
        or data
    )
    pool: list[dict] = list(raw_pool.values()) if isinstance(raw_pool, dict) else raw_pool

    now = datetime.now(timezone.utc).isoformat()
    players_rows: list[dict] = []
    next_match_rows: list[dict] = []
    seen_clubs: set[str] = set()

    # ── Sync clubs AVANT les joueurs (via /championship-clubs) ──
    known_club_ids: set[str] = {
        p.get("clubId") for p in pool if p.get("clubId")
    }
    clubs_resp = mpg.get("/championship-clubs")
    clubs_data = clubs_resp.json().get("championshipClubs", {}) if clubs_resp.status_code == 200 else {}
    clubs_rows: list[dict] = []
    for cid in known_club_ids:
        c = clubs_data.get(cid, {})
        raw_name = c.get("name") or {}
        name = raw_name.get("fr-FR") or raw_name.get("en-GB") or (raw_name if isinstance(raw_name, str) else None)
        clubs_rows.append({
            "id": cid,
            "name": name,
            "short_name": c.get("shortName"),
            "updated_at": now,
        })

    n = supa_upsert(supa, "l1_clubs", clubs_rows)
    print(f"  → {n} clubs upsertés dans l1_clubs")

    for p in pool:
        pid = p.get("id") or p.get("playerId")
        if not pid:
            continue

        ultra = p.get("ultraPosition", 0)
        club_id = p.get("clubId") or (p.get("club") or {}).get("id")

        # stats nestées dans "stats" ou aplaties au niveau joueur
        stats = p.get("stats") or {}

        def _stat(key: str, alt: str | None = None, default=0):
            v = p.get(key) if p.get(key) is not None else stats.get(key)
            if v is None and alt:
                v = p.get(alt) if p.get(alt) is not None else stats.get(alt)
            return v if v is not None else default

        players_rows.append({
            "id": pid,
            "first_name": p.get("firstName") or p.get("firstname") or "",
            "last_name": p.get("lastName") or p.get("lastname") or "",
            "club_id": club_id,
            "ultra_position": ultra,
            "position": ultra_to_pos(ultra),
            "quotation": p.get("quotation"),
            "quotation_trend": str(stats.get("quotationTrend") or p.get("quotationTrend") or ""),
            "average_rating": _stat("averageRating", default=None),
            "total_goals": _stat("totalGoals"),
            "total_assists": _stat("totalAssists"),
            "total_played": _stat("totalPlayedMatches", "totalMatches"),
            "total_started": _stat("totalStartedMatches", "totalStarted"),
            "total_yellow": _stat("totalYellowCards", "totalYellowCard"),
            "total_red": _stat("totalRedCards", "totalRedCard"),
            "updated_at": now,
        })

        # nextMatch : dans stats.nextMatch (nouvelle API) ou p.nextMatch (ancienne)
        nm = stats.get("nextMatch") or p.get("nextMatch") or p.get("next_match") or {}
        if nm and club_id and club_id not in seen_clubs:
            seen_clubs.add(club_id)
            # Déterminer opponent et is_home depuis la structure home/away
            side = nm.get("side")  # "home" ou "away"
            is_home = (side == "home") if side else nm.get("isHome")
            if is_home:
                opponent_id = (nm.get("away") or {}).get("clubId") or nm.get("opponentClubId")
            else:
                opponent_id = (nm.get("home") or {}).get("clubId") or nm.get("opponentClubId")
            next_match_rows.append({
                "club_id": club_id,
                "opponent_id": opponent_id,
                "is_home": is_home,
                "match_date": nm.get("date") or nm.get("matchDate"),
                "game_week": nm.get("gameWeekNumber") or nm.get("gameWeek"),
                "season": nm.get("season"),
                "updated_at": now,
            })

    n = supa_upsert(supa, "l1_players", players_rows)
    print(f"  → {n} joueurs upsertés dans l1_players")

    n = supa_upsert(supa, "l1_next_matches", next_match_rows)
    print(f"  → {n} clubs upsertés dans l1_next_matches")


# ── Étape 3 : Ratings L1 ─────────────────────────────────────────────────────

def _mark_gw_synced(season: int, gw: int) -> None:
    key = f"last_synced_gw::l1::{season}"
    current = get_manifest(key)
    if current is None or int(current) < gw:
        set_manifest(key, str(gw))


def _parse_match_ratings(mdata: dict, season: int, gw: int) -> list[dict]:
    """Extrait les ratings individuels depuis la réponse /championship-match/{id}."""
    rows: list[dict] = []
    match_date = mdata.get("date") or mdata.get("matchDate")

    for side in ("home", "away"):
        is_home = (side == "home")
        team = mdata.get(side) or mdata.get(f"{side}Team") or {}
        opp_side = "away" if is_home else "home"
        opp = mdata.get(opp_side) or mdata.get(f"{opp_side}Team") or {}
        opponent_club = opp.get("clubId") or opp.get("id")

        # players peut être un dict {player_id: {...}} ou une liste
        raw_players = team.get("players") or team.get("starters") or {}
        if isinstance(raw_players, dict):
            players_list = list(raw_players.values())
        else:
            players_list = raw_players

        for p in players_list:
            if not isinstance(p, dict):
                continue
            pid = p.get("id") or p.get("playerId")
            if not pid:
                continue
            pstats = p.get("stats") or {}
            rating = p.get("mpgRating") or p.get("rating") or p.get("score")
            rows.append({
                "player_id": pid,
                "season": season,
                "game_week": gw,
                "rating": rating,
                "goals": pstats.get("goals") or p.get("goals") or 0,
                "assists": pstats.get("goal_assist_intentional") or p.get("assists") or 0,
                "minutes_played": pstats.get("minutes_played") or p.get("minutesPlayed"),
                "is_home": is_home,
                "opponent_club": opponent_club,
                "match_date": match_date,
            })
    return rows


def sync_ratings(
    mpg: httpx.Client,
    supa: httpx.Client,
    seasons: list[int],
    gw_range: tuple[int, int] | None,
) -> None:
    print("[3] Sync ratings L1...")

    for season in seasons:
        gw_start, gw_end = gw_range if gw_range else (1, L1_TOTAL_GW)

        # Incrémental : reprendre après la dernière GW synced
        if gw_range is None:
            last = get_manifest(f"last_synced_gw::l1::{season}")
            if last is not None:
                gw_start = int(last) + 1
            if gw_start > gw_end:
                print(f"  Saison {season} : déjà à jour (GW {last})")
                continue

        print(f"  Saison {season} : GW{gw_start}→{gw_end}")

        for gw in range(gw_start, gw_end + 1):
            url = f"/championship-matches/1/season/{season}/game-week/{gw}"
            print(f"  [HTTP] GET {url}")
            resp = mpg.get(url)

            if resp.status_code == 404:
                print(f"  [STOP] Saison {season} GW{gw} — 404, fin de saison")
                break
            if resp.status_code != 200:
                print(f"  [WARN] Saison {season} GW{gw} — {resp.status_code}, on arrête")
                break

            gw_data = resp.json()
            matches_list = (
                gw_data.get("matches")
                or gw_data.get("championshipMatches")
                or (gw_data if isinstance(gw_data, list) else [])
            )
            match_ids = [
                m.get("id") or m.get("matchId")
                for m in matches_list
                if m.get("id") or m.get("matchId")
            ]

            if not match_ids:
                print(f"  [SKIP] Saison {season} GW{gw} — aucun match, fin de saison")
                break

            # Fetch chaque match individuel
            ratings_batch: list[dict] = []
            for mid in match_ids:
                mresp = mpg.get(f"/championship-match/{mid}")
                if mresp.status_code != 200:
                    print(f"  [WARN] match {mid} — {mresp.status_code}")
                    continue
                ratings_batch.extend(_parse_match_ratings(mresp.json(), season, gw))

            if ratings_batch:
                n = supa_upsert(supa, "l1_player_ratings", ratings_batch)
                print(f"    GW{gw} : {n} ratings upsertés")
                _mark_gw_synced(season, gw)
            else:
                print(f"    GW{gw} : aucun rating extrait (matchs peut-être pas encore joués)")


# ── Étape 4 : Rosters MPG ─────────────────────────────────────────────────────

def sync_rosters(supa: httpx.Client, division_id: str) -> None:
    print("[4] Sync rosters MPG depuis SQLite local...")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        """
        SELECT p.id    AS player_id,
               t.person_id,
               t.name  AS team_name,
               t.division_id,
               p.price
        FROM players p
        JOIN teams t ON p.team_id = t.id
        WHERE t.division_id = ?
          AND t.person_id IS NOT NULL
        """,
        (division_id,),
    ).fetchall()
    conn.close()

    roster_rows = [dict(r) for r in rows]
    n = supa_upsert(supa, "mpg_rosters", roster_rows)
    print(f"  → {n} entrées upsertées dans mpg_rosters")


# ── Étape 5 : Calendrier MPG ──────────────────────────────────────────────────

def sync_schedule(supa: httpx.Client, division_id: str) -> None:
    print("[5] Sync calendrier MPG (prochain match par joueur)...")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # Récupère tous les matchs non encore joués, les deux côtés (home + away)
    rows = conn.execute(
        """
        SELECT t.person_id, t.name AS team_name,
               opp.person_id AS opponent_id, opp.name AS opponent_name,
               m.game_week
        FROM matches m
        JOIN teams t   ON m.home_team_id = t.id
        JOIN teams opp ON m.away_team_id = opp.id
        WHERE m.division_id = ?
          AND m.home_score IS NULL
          AND t.person_id IS NOT NULL
        UNION ALL
        SELECT t.person_id, t.name AS team_name,
               opp.person_id AS opponent_id, opp.name AS opponent_name,
               m.game_week
        FROM matches m
        JOIN teams t   ON m.away_team_id = t.id
        JOIN teams opp ON m.home_team_id = opp.id
        WHERE m.division_id = ?
          AND m.home_score IS NULL
          AND t.person_id IS NOT NULL
        ORDER BY game_week ASC
        """,
        (division_id, division_id),
    ).fetchall()
    conn.close()

    if not rows:
        print("  [INFO] Aucun match à venir — saison terminée ?")
        return

    # On ne prend que la prochaine GW (minimum)
    next_gw = rows[0]["game_week"]
    now = datetime.now(timezone.utc).isoformat()
    seen: set[str] = set()
    schedule_rows: list[dict] = []

    for r in rows:
        if r["game_week"] != next_gw:
            break
        pid = r["person_id"]
        if pid and pid not in seen:
            seen.add(pid)
            schedule_rows.append({
                "person_id": pid,
                "team_name": r["team_name"],
                "opponent_id": r["opponent_id"],
                "opponent_name": r["opponent_name"],
                "game_week": next_gw,
                "season_finished": False,
                "updated_at": now,
            })

    n = supa_upsert(supa, "mpg_schedule", schedule_rows)
    print(f"  → {n} entrées upsertées dans mpg_schedule (GW{next_gw})")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Sync L1 MPG → Supabase")
    parser.add_argument("--full", action="store_true",
                        help="Backfill complet (saisons 2024+2025)")
    parser.add_argument("--season", type=int, default=None,
                        help="Saison spécifique (ex: 2024)")
    parser.add_argument("--gw", nargs=2, type=int, metavar=("START", "END"),
                        help="Range GW spécifique (ex: --gw 30 34)")
    args = parser.parse_args()

    division_id = _env("DIVISION_ID")

    mpg = build_mpg_client()
    supa = build_supa_client()

    with mpg, supa:
        # Étapes 1+2 : pool L1 + next matches (toujours)
        sync_pool(mpg, supa)

        # Étape 3 : ratings L1
        if args.gw:
            seasons = [args.season] if args.season else L1_SEASONS
            sync_ratings(mpg, supa, seasons, gw_range=(args.gw[0], args.gw[1]))
        elif args.full:
            sync_ratings(mpg, supa, L1_SEASONS, gw_range=None)
        else:
            # Incrémental : saison en cours seulement
            sync_ratings(mpg, supa, [2025], gw_range=None)

        # Étapes 4+5 : rosters + calendrier MPG
        sync_rosters(supa, division_id)
        sync_schedule(supa, division_id)

    print("\n[OK] Sync terminé.")


if __name__ == "__main__":
    main()
