"""MPG — initialisation SQLite et fonctions de persistance."""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent / "mpg.db"


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS league (
                id          TEXT PRIMARY KEY,
                name        TEXT,
                mode        TEXT,
                season      INTEGER,
                game_week_current INTEGER,
                raw_json    TEXT,
                fetched_at  TEXT
            );

            CREATE TABLE IF NOT EXISTS teams (
                id          TEXT PRIMARY KEY,
                division_id TEXT,
                name        TEXT,
                user_id     TEXT,
                budget      REAL,
                raw_json    TEXT,
                fetched_at  TEXT
            );

            CREATE TABLE IF NOT EXISTS players (
                id          TEXT PRIMARY KEY,
                team_id     TEXT,
                bid_date    TEXT,
                price       REAL,
                status      INTEGER,
                raw_json    TEXT,
                FOREIGN KEY (team_id) REFERENCES teams(id)
            );

            CREATE TABLE IF NOT EXISTS matches (
                id              TEXT PRIMARY KEY,
                game_week       INTEGER,
                home_team_id    TEXT,
                away_team_id    TEXT,
                home_score      REAL,
                away_score      REAL,
                home_bonuses    TEXT,
                away_bonuses    TEXT,
                is_finalized    INTEGER DEFAULT 0,
                raw_json        TEXT,
                fetched_at      TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_matches_gw ON matches(game_week);

            CREATE TABLE IF NOT EXISTS manifest (
                key         TEXT PRIMARY KEY,
                value       TEXT
            );

            CREATE TABLE IF NOT EXISTS divisions_metadata (
                division_id       TEXT PRIMARY KEY,
                season            INTEGER,
                is_covid          INTEGER DEFAULT 0,
                is_incomplete     INTEGER DEFAULT 0,
                expected_matches  INTEGER DEFAULT 56,
                n_matches         INTEGER,
                gw_min            INTEGER,
                gw_max            INTEGER,
                notes             TEXT
            );
        """)
        # Migration players : recréer si ancien schéma (sans bid_date)
        try:
            conn.execute("SELECT bid_date FROM players LIMIT 1")
        except sqlite3.OperationalError:
            conn.execute("DROP TABLE IF EXISTS players")
            conn.execute("""
                CREATE TABLE players (
                    id          TEXT PRIMARY KEY,
                    team_id     TEXT,
                    bid_date    TEXT,
                    price       REAL,
                    status      INTEGER,
                    raw_json    TEXT,
                    FOREIGN KEY (team_id) REFERENCES teams(id)
                )
            """)
            print("[DB] Migration players : schéma mis à jour")
        # Migration matches : ajouter is_finalized si absent
        try:
            conn.execute("SELECT is_finalized FROM matches LIMIT 1")
        except sqlite3.OperationalError:
            conn.execute("ALTER TABLE matches ADD COLUMN is_finalized INTEGER DEFAULT 0")
            print("[DB] Migration matches : colonne is_finalized ajoutée")
        # Migration matches : ajouter division_id si absent
        try:
            conn.execute("SELECT division_id FROM matches LIMIT 1")
        except sqlite3.OperationalError:
            conn.execute("ALTER TABLE matches ADD COLUMN division_id TEXT")
            print("[DB] Migration matches : colonne division_id ajoutée")
        # Migration matches : ajouter season si absent + backfill depuis raw_json
        try:
            conn.execute("SELECT season FROM matches LIMIT 1")
        except sqlite3.OperationalError:
            conn.execute("ALTER TABLE matches ADD COLUMN season INTEGER")
            conn.execute("""
                UPDATE matches
                SET season = CAST(json_extract(raw_json, '$.championshipSeason') AS INTEGER)
                WHERE season IS NULL AND raw_json IS NOT NULL
            """)
            print("[DB] Migration matches : colonne season ajoutée + backfill")
        # Index composite — créé après la migration de season
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_matches_season "
            "ON matches(season, division_id, game_week)"
        )
        # Migration teams : ajouter person_id si absent
        try:
            conn.execute("SELECT person_id FROM teams LIMIT 1")
        except sqlite3.OperationalError:
            conn.execute("ALTER TABLE teams ADD COLUMN person_id TEXT")
            print("[DB] Migration teams : colonne person_id ajoutée")
        # Migration divisions_metadata : ajouter is_current si absent
        try:
            conn.execute("SELECT is_current FROM divisions_metadata LIMIT 1")
        except sqlite3.OperationalError:
            conn.execute("ALTER TABLE divisions_metadata ADD COLUMN is_current INTEGER DEFAULT 0")
            print("[DB] Migration divisions_metadata : colonne is_current ajoutée")
    print("[DB] Tables initialisées")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def save_league(data: dict) -> None:
    row = (
        data.get("id"),
        data.get("name"),
        data.get("mode"),
        data.get("season"),
        data.get("gameWeekCurrent") or data.get("currentGameWeek"),
        json.dumps(data),
        _now(),
    )
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO league (id, name, mode, season, game_week_current, raw_json, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name=excluded.name, mode=excluded.mode, season=excluded.season,
                game_week_current=excluded.game_week_current,
                raw_json=excluded.raw_json, fetched_at=excluded.fetched_at
        """, row)
    print(f"[DB] League sauvegardée : {data.get('name', data.get('id'))}")


def save_teams(division_id: str, teams: list[dict]) -> None:
    now = _now()
    with get_conn() as conn:
        for team in teams:
            conn.execute("""
                INSERT INTO teams (id, division_id, name, user_id, budget, raw_json, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    division_id=excluded.division_id, name=excluded.name,
                    user_id=excluded.user_id, budget=excluded.budget,
                    raw_json=excluded.raw_json, fetched_at=excluded.fetched_at
            """, (
                team.get("id"),
                division_id,
                team.get("name"),
                team.get("userId"),
                team.get("budget"),
                json.dumps(team),
                now,
            ))

            # Players : squad est un dict {mpg_championship_player_XXX: {bidDate, price, status}}
            squad = team.get("squad", {})
            player_count = 0
            for player_id, player_data in squad.items():
                if not player_id.startswith("mpg_championship_player_"):
                    continue
                conn.execute("""
                    INSERT INTO players (id, team_id, bid_date, price, status, raw_json)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        team_id=excluded.team_id, bid_date=excluded.bid_date,
                        price=excluded.price, status=excluded.status,
                        raw_json=excluded.raw_json
                """, (
                    player_id,
                    team.get("id"),
                    player_data.get("bidDate"),
                    player_data.get("price"),
                    player_data.get("status"),
                    json.dumps(player_data),
                ))
                player_count += 1

    total_players = sum(
        len([k for k in t.get("squad", {}) if k.startswith("mpg_championship_player_")])
        for t in teams
    )
    print(f"[DB] {len(teams)} équipes sauvegardées — {total_players} joueurs (division {division_id})")


def save_matches(game_week: int, matches: list[dict], division_id: str) -> None:
    now = _now()
    with get_conn() as conn:
        for m in matches:
            home = m.get("home") or m.get("homeTeam") or {}
            away = m.get("away") or m.get("awayTeam") or {}
            season = m.get("championshipSeason")
            conn.execute("""
                INSERT INTO matches
                    (id, game_week, season, division_id, home_team_id, away_team_id,
                     home_score, away_score, home_bonuses, away_bonuses,
                     raw_json, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    season=excluded.season, division_id=excluded.division_id,
                    home_score=excluded.home_score, away_score=excluded.away_score,
                    home_bonuses=excluded.home_bonuses, away_bonuses=excluded.away_bonuses,
                    raw_json=excluded.raw_json, fetched_at=excluded.fetched_at
            """, (
                m.get("id"),
                m.get("gameWeek") or game_week,
                season,
                division_id,
                home.get("teamId"),
                away.get("teamId"),
                home.get("score"),
                away.get("score"),
                json.dumps(home.get("bonuses", {})),
                json.dumps(away.get("bonuses", {})),
                json.dumps(m),
                now,
            ))
    print(f"[DB] GW{game_week} — {len(matches)} matchs sauvegardés [{division_id}]")


def mark_finalized_up_to(gw: int, division_id: str) -> None:
    """Marque is_finalized=1 pour toutes les GW <= gw de la division."""
    with get_conn() as conn:
        result = conn.execute(
            "UPDATE matches SET is_finalized=1 WHERE game_week <= ? AND division_id=? AND is_finalized=0",
            (gw, division_id),
        )
        if result.rowcount:
            print(f"[DB] GW1→GW{gw} marquées finalisées ({result.rowcount} matchs) [{division_id}]")


def get_last_fetched_game_week(division_id: str) -> int:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT MAX(game_week) AS max_gw FROM matches WHERE division_id=?",
            (division_id,),
        ).fetchone()
        return row["max_gw"] or 0


def get_league_current_game_week() -> int:
    with get_conn() as conn:
        row = conn.execute("SELECT game_week_current FROM league LIMIT 1").fetchone()
        return row["game_week_current"] or 1


def set_manifest(key: str, value: str) -> None:
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO manifest (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """, (key, value))


def get_manifest(key: str) -> str | None:
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM manifest WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None


# Divisions tronquées par le COVID — taguées manuellement car is_incomplete seul
# ne permet pas de distinguer "pas encore terminée" d'une "coupée par un événement externe".
COVID_DIVISIONS: frozenset[str] = frozenset({
    "mpg_division_QU0SUZ6HQPB_6_1",
})

# Saison en cours — exclue des stats historiques (palmarès, chapeaux, podiums).
# À mettre à jour manuellement à chaque nouvelle saison.
CURRENT_DIVISION: str = "mpg_division_QU0SUZ6HQPB_18_1"


def refresh_divisions_metadata(
    expected_matches: int = 56,
    covid_divisions: frozenset[str] | None = None,
    current_division: str | None = None,
) -> None:
    """Recalcule divisions_metadata depuis matches (upsert).

    - is_incomplete=1 si n_matches < expected_matches
    - is_covid=1 si division_id dans covid_divisions (liste explicite)
    - is_current=1 si division_id == current_division
    """
    if covid_divisions is None:
        covid_divisions = COVID_DIVISIONS
    if current_division is None:
        current_division = CURRENT_DIVISION

    with get_conn() as conn:
        rows = conn.execute("""
            SELECT division_id,
                   MAX(season)    AS season,
                   COUNT(*)       AS n_matches,
                   MIN(game_week) AS gw_min,
                   MAX(game_week) AS gw_max
            FROM matches
            WHERE division_id IS NOT NULL
            GROUP BY division_id
        """).fetchall()

        for r in rows:
            div_id = r["division_id"]
            conn.execute("""
                INSERT INTO divisions_metadata
                    (division_id, season, is_covid, is_incomplete, is_current,
                     expected_matches, n_matches, gw_min, gw_max)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(division_id) DO UPDATE SET
                    season=excluded.season,
                    is_covid=excluded.is_covid,
                    is_incomplete=excluded.is_incomplete,
                    is_current=excluded.is_current,
                    expected_matches=excluded.expected_matches,
                    n_matches=excluded.n_matches,
                    gw_min=excluded.gw_min,
                    gw_max=excluded.gw_max
            """, (
                div_id,
                r["season"],
                1 if div_id in covid_divisions else 0,
                1 if r["n_matches"] < expected_matches else 0,
                1 if div_id == current_division else 0,
                expected_matches,
                r["n_matches"],
                r["gw_min"],
                r["gw_max"],
            ))

    n = len(rows)
    print(f"[DB] divisions_metadata : {n} division(s) mises à jour")


def get_excluded_divisions(
    include_covid: bool = False,
    include_incomplete: bool = False,
    include_current: bool = False,
) -> list[str]:
    """Retourne les division_ids à exclure des stats selon les flags."""
    with get_conn() as conn:
        clauses, params = [], []
        if not include_covid:
            clauses.append("is_covid=1")
        if not include_incomplete:
            clauses.append("is_incomplete=1")
        if not include_current:
            clauses.append("is_current=1")
        if not clauses:
            return []
        where = " OR ".join(clauses)
        rows = conn.execute(
            f"SELECT division_id FROM divisions_metadata WHERE {where}", params
        ).fetchall()
    return [r["division_id"] for r in rows]
