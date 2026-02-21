"""MPG — calcul déterministe des bonus restants par équipe.

Logique : rejouer l'historique des matchs depuis la DB.
Seuls les bonus consommables (BONUS_CATALOG[...]["is_consumable"] == True) entrent
dans le rapport. Les stocks viennent du catalogue centralisé.
"""

import json
from collections import defaultdict
from mpg_db import get_conn
from bonus_catalog import BONUS_CATALOG, CONSUMABLE_KEYS, format_bonus_name  # noqa: F401


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
