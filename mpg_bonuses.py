"""MPG — calcul déterministe des bonus restants par équipe.

Logique : rejouer l'historique des matchs depuis la DB.
Seuls les bonus consommables (BONUS_CATALOG[...]["is_consumable"] == True) entrent
dans le rapport. Les stocks viennent du catalogue centralisé.
"""

import json
from collections import defaultdict
from mpg_db import get_conn
from bonus_catalog import BONUS_CATALOG, CONSUMABLE_KEYS, format_bonus_name  # noqa: F401

NON_CONSUMABLE = {"captain", "boostDefense4", "boostDefense5"}

# Impact du Miroir sur chaque bonus (ce que l'adversaire reçoit si il joue Miroir)
_MIRROR_IMPACT = {
    "boostOnePlayer":    "+1 à un de SES joueurs",
    "boostAllPlayers":   "+0.5 à TOUS ses titulaires",
    "removeGoal":        "retire un de TES buts",
    "fourStrikers":      "+0.5 à SES défenseurs",
    "blockTacticalSubs": "TES remplacements bloqués",
    "nerfGoalkeeper":    "-1 à TON gardien",
    "nerfAllPlayers":    "-0.5 à TOUS tes joueurs",
}


def count_bonuses_used(
    up_to_gw: int | None = None,
    division_id: str | None = None,
) -> dict[str, dict[str, int]]:
    """Retourne {team_id: {bonus_type: count}} en rejouant l'historique.

    Args:
        up_to_gw: si fourni, ne compte que les GW <= up_to_gw.
        division_id: si fourni, ne compte que les matchs de cette division.
    """
    used: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    filters, params = [], []
    if up_to_gw is not None:
        filters.append("game_week <= ?")
        params.append(up_to_gw)
    if division_id is not None:
        filters.append("division_id = ?")
        params.append(division_id)

    query = "SELECT home_team_id, away_team_id, home_bonuses, away_bonuses FROM matches"
    if filters:
        query += " WHERE " + " AND ".join(filters)

    with get_conn() as conn:
        rows = conn.execute(query, params).fetchall()

    for row in rows:
        for team_id, bonuses_json in (
            (row["home_team_id"], row["home_bonuses"]),
            (row["away_team_id"], row["away_bonuses"]),
        ):
            if not team_id or not bonuses_json:
                continue
            bonuses: dict = json.loads(bonuses_json)
            for bonus_type in bonuses:
                used[team_id][bonus_type] += 1   # générique : pas de filtre ici

    return {team_id: dict(counts) for team_id, counts in used.items()}


def compute_remaining_bonuses(
    up_to_gw: int | None = None,
    division_id: str | None = None,
) -> dict[str, dict[str, dict[str, int]]]:
    """Retourne {team_id: {api_key: {"used": n, "total": n, "remaining": n}}}.

    Seuls les bonus consommables du catalogue sont inclus.
    """
    used_by_team = count_bonuses_used(up_to_gw=up_to_gw, division_id=division_id)

    with get_conn() as conn:
        query = "SELECT id FROM teams"
        params: tuple = ()
        if division_id:
            query += " WHERE division_id=?"
            params = (division_id,)
        all_teams = [row["id"] for row in conn.execute(query, params).fetchall()]

    result: dict[str, dict[str, dict[str, int]]] = {}
    for team_id in all_teams:
        team_used = used_by_team.get(team_id, {})
        result[team_id] = {}
        for api_key in CONSUMABLE_KEYS:
            total = BONUS_CATALOG[api_key]["stock_default"]
            n_used = team_used.get(api_key, 0)
            result[team_id][api_key] = {
                "used":      n_used,
                "total":     total,
                "remaining": max(0, total - n_used),
            }

    return result


def print_bonus_report(
    up_to_gw: int | None = None,
    division_id: str | None = None,
) -> None:
    """Affiche le rapport des bonus restants (consommables uniquement, labels UI)."""
    with get_conn() as conn:
        query = "SELECT id, name FROM teams"
        params: tuple = ()
        if division_id:
            query += " WHERE division_id=?"
            params = (division_id,)
        team_names = {row["id"]: row["name"] for row in conn.execute(query, params).fetchall()}

    remaining = compute_remaining_bonuses(up_to_gw=up_to_gw, division_id=division_id)

    # En-têtes : label UI (clé technique en suffixe pour debug)
    col_keys  = CONSUMABLE_KEYS
    col_labels = [f"{BONUS_CATALOG[k]['ui_label']}" for k in col_keys]
    col_w = max(len(l) + 12 for l in col_labels)  # largeur colonne = label + "x/y (u:z)"

    div_label = f" | {division_id}" if division_id else ""
    gw_label  = f"jusqu'à GW{up_to_gw}" if up_to_gw else "toute la saison"
    print(f"\n=== Bonus restants ({gw_label}{div_label}) ===")

    header = f"{'Équipe':<30}" + "".join(f"{l:^{col_w}}" for l in col_labels)
    print(header)
    print("-" * len(header))

    # Filtrer aux équipes connues + trier par nom
    items = [(tid, b) for tid, b in remaining.items() if tid in team_names]
    for team_id, bonuses in sorted(items, key=lambda x: team_names[x[0]]):
        name = team_names[team_id]
        row_str = f"{name:<30}"
        for k in col_keys:
            info  = bonuses[k]
            cell  = f"{info['remaining']}/{info['total']} (u:{info['used']})"
            row_str += f"{cell:^{col_w}}"
        print(row_str)
    print()


def print_bonus_advice(division_id: str, my_person_id: str = "raph") -> None:
    """Analyse les bonus pour la prochaine journée : ce qu'il reste, ce que l'adversaire a,
    son historique contre moi, et une recommandation tenant compte du risque Miroir."""

    with get_conn() as conn:
        # Mon équipe dans cette division
        my_team = conn.execute(
            "SELECT id, name FROM teams WHERE division_id=? AND person_id=?",
            (division_id, my_person_id),
        ).fetchone()
        if not my_team:
            print(f"[BONUS ADVICE] Joueur '{my_person_id}' introuvable dans {division_id}")
            return
        my_team_id, my_team_name = my_team["id"], my_team["name"]

        # GW en cours (max avec score) → prochain = +1
        current_gw = conn.execute(
            "SELECT MAX(game_week) AS g FROM matches WHERE division_id=? AND home_score IS NOT NULL",
            (division_id,),
        ).fetchone()["g"] or 0
        next_gw = current_gw + 1

        # Prochain adversaire
        match = conn.execute(
            """SELECT home_team_id, away_team_id FROM matches
               WHERE division_id=? AND game_week=?
               AND (home_team_id=? OR away_team_id=?)""",
            (division_id, next_gw, my_team_id, my_team_id),
        ).fetchone()
        if not match:
            print(f"[BONUS ADVICE] Aucun match trouvé pour J{next_gw}")
            return

        opp_team_id = match["away_team_id"] if match["home_team_id"] == my_team_id else match["home_team_id"]
        opp_team = conn.execute(
            "SELECT person_id, name FROM teams WHERE id=?", (opp_team_id,)
        ).fetchone()
        opp_person_id = opp_team["person_id"]
        opp_team_name = opp_team["name"]

        # Tous les team_ids de l'adversaire (toutes divisions/saisons) pour l'historique
        opp_all_ids = [
            r["id"] for r in conn.execute(
                "SELECT id FROM teams WHERE person_id=?", (opp_person_id,)
            ).fetchall()
        ]
        my_all_ids = [
            r["id"] for r in conn.execute(
                "SELECT id FROM teams WHERE person_id=?", (my_person_id,)
            ).fetchall()
        ]

        # Historique bonus adversaire contre moi (toutes saisons)
        history: list[tuple[int, str, list[str]]] = []
        if opp_all_ids and my_all_ids:
            placeholders_opp = ",".join("?" * len(opp_all_ids))
            placeholders_my  = ",".join("?" * len(my_all_ids))
            rows = conn.execute(
                f"""SELECT game_week, home_team_id, away_team_id,
                           home_bonuses, away_bonuses,
                           dm.season
                    FROM matches m
                    JOIN divisions_metadata dm ON m.division_id = dm.division_id
                    WHERE ((m.home_team_id IN ({placeholders_opp}) AND m.away_team_id IN ({placeholders_my}))
                        OR (m.home_team_id IN ({placeholders_my}) AND m.away_team_id IN ({placeholders_opp})))
                    AND m.home_score IS NOT NULL
                    ORDER BY dm.season, m.game_week""",
                opp_all_ids + my_all_ids + my_all_ids + opp_all_ids,
            ).fetchall()

            for r in rows:
                if r["home_team_id"] in opp_all_ids:
                    bonuses_json = r["home_bonuses"]
                else:
                    bonuses_json = r["away_bonuses"]
                bonuses = [k for k in (json.loads(bonuses_json) if bonuses_json else {})
                           if k not in NON_CONSUMABLE]
                history.append((r["season"], r["game_week"], bonuses))

    # Bonus restants (saison courante)
    remaining = compute_remaining_bonuses(division_id=division_id)
    my_remaining   = {k: v for k, v in remaining.get(my_team_id, {}).items() if v["remaining"] > 0}
    opp_remaining  = {k: v for k, v in remaining.get(opp_team_id, {}).items() if v["remaining"] > 0}

    opp_has_mirror = "mirror" in opp_remaining

    # ── Affichage ──────────────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"  CONSEIL BONUS — J{next_gw} : {my_team_name} vs {opp_team_name}")
    print(f"{'='*60}")

    # Mes bonus restants
    print(f"\n▶ Tes bonus restants :")
    if my_remaining:
        for k, v in my_remaining.items():
            label = BONUS_CATALOG[k]["ui_label"]
            stock = f"(x{v['remaining']})" if v["remaining"] > 1 else ""
            print(f"   • {label} {stock}")
    else:
        print("   (aucun)")

    # Bonus adversaire restants
    print(f"\n▶ Bonus restants de {opp_person_id} :")
    if opp_remaining:
        for k, v in opp_remaining.items():
            label = BONUS_CATALOG[k]["ui_label"]
            stock = f"(x{v['remaining']})" if v["remaining"] > 1 else ""
            mirror_warn = " ⚠️  MIROIR" if k == "mirror" else ""
            print(f"   • {label} {stock}{mirror_warn}")
    else:
        print("   (aucun)")

    # Historique adversaire vs moi
    print(f"\n▶ Historique bonus de {opp_person_id} contre toi ({len(history)} matchs joués) :")
    bonus_counts: dict[str, int] = defaultdict(int)
    for season, gw, bonuses in history[-10:]:  # 10 dernières saisons
        label = ", ".join(BONUS_CATALOG[b]["ui_label"] if b in BONUS_CATALOG else b for b in bonuses) or "aucun"
        print(f"   S{season} J{gw} : {label}")
        for b in bonuses:
            bonus_counts[b] += 1

    if bonus_counts:
        print(f"\n   Top utilisés : " + ", ".join(
            f"{BONUS_CATALOG[k]['ui_label'] if k in BONUS_CATALOG else k} ({n}x)"
            for k, n in sorted(bonus_counts.items(), key=lambda x: -x[1])
        ))

    # Recommandation
    print(f"\n▶ Recommandation :")
    if opp_has_mirror:
        print(f"   ⚠️  {opp_person_id} a encore son Miroir — tout bonus que tu joues peut se retourner contre toi :")
        for k in my_remaining:
            impact = _MIRROR_IMPACT.get(k)
            if impact:
                label = BONUS_CATALOG[k]["ui_label"]
                print(f"   • {label} → si Miroir : {impact}")
        print(f"\n   → Conseil : ne rien jouer (le Miroir est gaspillé) ou accepter le risque.")
    else:
        print(f"   Pas de Miroir chez {opp_person_id}.")
        # Classer les bonus par impact décroissant (ordre subjectif fixe)
        impact_order = ["nerfAllPlayers", "nerfGoalkeeper", "removeGoal", "boostAllPlayers",
                        "boostOnePlayer", "fourStrikers", "blockTacticalSubs"]
        ranked = [k for k in impact_order if k in my_remaining]
        if ranked:
            best = ranked[0]
            label = BONUS_CATALOG[best]["ui_label"]
            print(f"   → Meilleur choix : {label}")
        else:
            print("   → Aucun bonus disponible.")
    print()
