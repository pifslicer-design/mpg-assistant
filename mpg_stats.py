"""MPG — statistiques dérivées de l'historique.

Divisions COVID et incomplètes exclues par défaut (via divisions_metadata).
"""

import json
from collections import defaultdict
from mpg_db import get_conn, get_excluded_divisions

COVID_SEASON = 6  # conservé pour rétro-compat (utilisé dans test_batch_import)


def compute_records(
    division_id: str | None = None,
    include_covid: bool = False,
    include_incomplete: bool = False,
) -> dict:
    """Calcule W/D/L, points, score moyen par équipe.

    finalResult dans raw_json : 1=home win, 2=draw, 3=away win.
    Retourne {team_id: {wins, draws, losses, points, goals_for, goals_against,
                        matches_played, avg_score, person_id, team_name}}.
    """
    excluded = get_excluded_divisions(
        include_covid=include_covid,
        include_incomplete=include_incomplete,
    )

    filters = ["1=1"]
    params: list = []
    if division_id:
        filters.append("m.division_id=?")
        params.append(division_id)
    if excluded:
        placeholders = ",".join("?" * len(excluded))
        filters.append(f"m.division_id NOT IN ({placeholders})")
        params.extend(excluded)
    filters_sql = " AND ".join(filters)

    with get_conn() as conn:
        rows = conn.execute(
            f"""SELECT m.id, m.home_team_id, m.away_team_id,
                       m.home_score, m.away_score, m.raw_json,
                       ht.name AS home_name, ht.person_id AS home_person,
                       at.name AS away_name, at.person_id AS away_person
                FROM matches m
                LEFT JOIN teams ht ON m.home_team_id = ht.id
                LEFT JOIN teams at ON m.away_team_id = at.id
                WHERE {filters_sql}""",
            params,
        ).fetchall()

    stats: dict = defaultdict(lambda: {
        "wins": 0, "draws": 0, "losses": 0, "points": 0,
        "goals_for": 0.0, "goals_against": 0.0, "matches_played": 0,
        "team_name": "", "person_id": None,
    })

    for r in rows:
        raw = json.loads(r["raw_json"] or "{}")
        result = raw.get("finalResult")  # 1=home win, 2=draw, 3=away win
        h, a = r["home_team_id"], r["away_team_id"]

        if not h or not a or result not in (1, 2, 3):
            continue

        for team_id, is_home in ((h, True), (a, False)):
            s = stats[team_id]
            s["team_name"]    = r["home_name"] if is_home else r["away_name"]
            s["person_id"]    = r["home_person"] if is_home else r["away_person"]
            s["matches_played"] += 1
            gf = (r["home_score"] or 0) if is_home else (r["away_score"] or 0)
            ga = (r["away_score"] or 0) if is_home else (r["home_score"] or 0)
            s["goals_for"]     += gf
            s["goals_against"] += ga

            if result == 2:
                s["draws"]  += 1
                s["points"] += 1
            elif (result == 1 and is_home) or (result == 3 and not is_home):
                s["wins"]   += 1
                s["points"] += 3
            else:
                s["losses"] += 1

    # Calcul score moyen
    for s in stats.values():
        mp = s["matches_played"]
        s["avg_score"] = round(s["goals_for"] / mp, 2) if mp else 0.0

    return dict(stats)


def print_stats_report(
    division_id: str | None = None,
    include_covid: bool = False,
    include_incomplete: bool = False,
) -> None:
    """Affiche le classement W/D/L par équipe."""
    records = compute_records(
        division_id=division_id,
        include_covid=include_covid,
        include_incomplete=include_incomplete,
    )
    if not records:
        print("[STATS] Aucune donnée.")
        return

    exclusions = []
    if not include_covid:
        exclusions.append("COVID exclus")
    if not include_incomplete:
        exclusions.append("incomplets exclus")
    excl_label = ", ".join(exclusions) or "tout inclus"
    div_label  = f" | {division_id}" if division_id else ""
    print(f"\n=== Classement ({excl_label}{div_label}) ===")

    col = 24
    header = f"{'Équipe':<{col}} {'Pers.':<10} {'J':>3} {'V':>3} {'N':>3} {'D':>3} {'Pts':>4} {'Moy':>6}"
    print(header)
    print("-" * len(header))

    for team_id, s in sorted(records.items(), key=lambda x: -x[1]["points"]):
        name    = (s["person_id"] or s["team_name"] or team_id)[:col - 1]
        team_lbl = s["team_name"][:9]
        print(
            f"{name:<{col}} {team_lbl:<10} "
            f"{s['matches_played']:>3} {s['wins']:>3} {s['draws']:>3} {s['losses']:>3} "
            f"{s['points']:>4} {s['avg_score']:>6.2f}"
        )
    print()
