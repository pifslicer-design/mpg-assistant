"""event_detector.py — Détecteurs d'événements remarquables pour le résumé post-journée.

Produit une liste structurée d'événements survenus à la dernière journée terminée
de la division en cours. Cette liste est ensuite passée à summary_writer.py qui
la transforme en texte (LLM ou templates).

Détecteurs MVP :
  - statut champion / chapeau (mathématiquement scellé, première fois)
  - record all-time : plus large score absolu, plus large H2H X sur Y
  - records saison en cours : pts, V, D, GD, BP, BC (battus à cette journée)
  - records de séries V/NV/L (égalisations + dépassements)
  - scores extrêmes de la journée (plus large écart, total)
  - chatte hebdo extrême (best/worst delta sur la journée)
  - bonus décisifs (qui ont changé W/D/L)
"""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Optional

from mpg_db import get_conn  # noqa: F401  (pour cohérence d'API si besoin)
from mpg_legacy_engine import (
    _load_display_names,
    compute_streaks,
    list_included_divisions,
)
# PLAYER_COLORS / PLAYER_ORDER / slabel vivent dans generate_pages.py.
# Import retardé pour éviter les cycles (generate_pages importe ce module).
from generate_pages import PLAYER_COLORS, PLAYER_ORDER, slabel  # type: ignore

# Pour le contrefactuel "et si pas de bonus"
from mpg_goal_engine import simulate_without_bonus  # type: ignore

MAX_PTS_PER_MATCH = 5  # 3 (victoire) + 2 (bonus mass goals max)


# ──────────────────────────────────────────────────────────────────────────────
# Identification de la dernière journée terminée
# ──────────────────────────────────────────────────────────────────────────────

def find_last_finalized_gw(conn) -> Optional[dict]:
    """Retourne {season, division_id, game_week, slabel} de la dernière journée
    entièrement finalisée dans la division en cours, ou None si aucune."""
    row = conn.execute(
        "SELECT division_id, season FROM divisions_metadata WHERE is_current=1 LIMIT 1"
    ).fetchone()
    if not row:
        return None
    div_id = row["division_id"]
    season = row["season"]

    # Dernière gw où TOUS les matchs de la journée sont finalisés
    gw_row = conn.execute(
        """
        SELECT game_week, COUNT(*) AS total, SUM(is_finalized) AS done
        FROM matches WHERE division_id=?
        GROUP BY game_week
        HAVING done = total
        ORDER BY game_week DESC
        LIMIT 1
        """,
        (div_id,),
    ).fetchone()
    if not gw_row:
        return None
    return {
        "season": season,
        "division_id": div_id,
        "game_week": gw_row["game_week"],
        "slabel": slabel(div_id),
    }


# ──────────────────────────────────────────────────────────────────────────────
# Helpers communs
# ──────────────────────────────────────────────────────────────────────────────

def _team_pid_map(conn, division_id: str) -> dict[str, str]:
    """{team_id: person_id} pour la division donnée."""
    rows = conn.execute(
        "SELECT id, person_id FROM teams WHERE division_id=? AND person_id IS NOT NULL",
        (division_id,),
    ).fetchall()
    return {r["id"]: r["person_id"] for r in rows}


def _gw_matches(conn, division_id: str, game_week: int) -> list:
    """Tous les matchs d'une journée (objets sqlite Row)."""
    return conn.execute(
        "SELECT * FROM matches WHERE division_id=? AND game_week=? AND is_finalized=1",
        (division_id, game_week),
    ).fetchall()


def _player_card(pid: str, display: dict) -> dict:
    return {
        "pid":   pid,
        "name":  display.get(pid, pid),
        "color": PLAYER_COLORS.get(pid, "#888"),
    }


# ──────────────────────────────────────────────────────────────────────────────
# 1. Statut champion / chapeau (mathématiquement scellé pour la 1re fois)
# ──────────────────────────────────────────────────────────────────────────────

def _standings_at_gw(conn, division_id: str, max_gw: int) -> list[dict]:
    """Classement (pts, V, N, D, BP, BC, GD) calculé jusqu'à max_gw inclus.

    Ordre : pts DESC, GD DESC, BP DESC.
    """
    pid_map = _team_pid_map(conn, division_id)
    rows = conn.execute(
        """
        SELECT home_team_id, away_team_id, home_score, away_score, game_week
        FROM matches WHERE division_id=? AND is_finalized=1 AND game_week<=?
        """,
        (division_id, max_gw),
    ).fetchall()
    s = {pid: {"pid": pid, "pts": 0, "v": 0, "n": 0, "d": 0,
               "bp": 0, "bc": 0, "mp": 0} for pid in pid_map.values()}
    for r in rows:
        hp = pid_map.get(r["home_team_id"])
        ap = pid_map.get(r["away_team_id"])
        if not hp or not ap:
            continue
        hs, as_ = r["home_score"] or 0, r["away_score"] or 0
        s[hp]["mp"] += 1
        s[ap]["mp"] += 1
        s[hp]["bp"] += hs
        s[hp]["bc"] += as_
        s[ap]["bp"] += as_
        s[ap]["bc"] += hs
        if hs > as_:
            s[hp]["pts"] += 3; s[hp]["v"] += 1; s[ap]["d"] += 1
        elif hs < as_:
            s[ap]["pts"] += 3; s[ap]["v"] += 1; s[hp]["d"] += 1
        else:
            s[hp]["pts"] += 1; s[ap]["pts"] += 1
            s[hp]["n"] += 1;   s[ap]["n"] += 1
    rows = list(s.values())
    for r in rows:
        r["gd"] = r["bp"] - r["bc"]
    rows.sort(key=lambda r: (-r["pts"], -r["gd"], -r["bp"]))
    return rows


def _is_champion_sealed(standings: list[dict], total_gw: int) -> Optional[str]:
    """Retourne le pid du champion si scellé, None sinon."""
    if len(standings) < 2:
        return None
    leader = standings[0]
    rest = standings[1:]
    matches_left_of_leader = total_gw - leader["mp"]
    # Leader DOIT être au-dessus de tous les autres même si chaque autre fait
    # le maximum sur ses matchs restants (et le leader 0 pt sur les siens).
    for other in rest:
        ml = total_gw - other["mp"]
        max_other = other["pts"] + ml * MAX_PTS_PER_MATCH
        if max_other >= leader["pts"]:
            return None
    return leader["pid"]


def _is_chapeau_sealed(standings: list[dict], total_gw: int) -> Optional[str]:
    """Retourne le pid du chapeau si scellé, None sinon."""
    if len(standings) < 2:
        return None
    last = standings[-1]
    rest = standings[:-1]
    matches_left_last = total_gw - last["mp"]
    max_last = last["pts"] + matches_left_last * MAX_PTS_PER_MATCH
    for other in rest:
        # Le chapeau (dernier) doit être en dessous de TOUS même au max théorique
        if other["pts"] <= max_last:
            return None
    return last["pid"]


def detect_season_status(conn, gw_info: dict, total_gw: int = 14) -> list[dict]:
    """Champion ou chapeau scellé pour la 1re fois à cette journée."""
    div_id = gw_info["division_id"]
    gw = gw_info["game_week"]
    display = _load_display_names()
    events: list[dict] = []

    standings_now = _standings_at_gw(conn, div_id, gw)
    standings_prev = _standings_at_gw(conn, div_id, gw - 1) if gw > 1 else []

    champion_now = _is_champion_sealed(standings_now, total_gw)
    champion_prev = _is_champion_sealed(standings_prev, total_gw) if standings_prev else None
    if champion_now and champion_now != champion_prev:
        events.append({
            "type": "champion_sealed",
            "pid": champion_now,
            **_player_card(champion_now, display),
            "pts": next(r["pts"] for r in standings_now if r["pid"] == champion_now),
            "gw": gw,
            "slabel": gw_info["slabel"],
        })

    chapeau_now = _is_chapeau_sealed(standings_now, total_gw)
    chapeau_prev = _is_chapeau_sealed(standings_prev, total_gw) if standings_prev else None
    if chapeau_now and chapeau_now != chapeau_prev:
        events.append({
            "type": "chapeau_sealed",
            "pid": chapeau_now,
            **_player_card(chapeau_now, display),
            "pts": next(r["pts"] for r in standings_now if r["pid"] == chapeau_now),
            "gw": gw,
            "slabel": gw_info["slabel"],
        })

    return events


# ──────────────────────────────────────────────────────────────────────────────
# 2. Records all-time touchés sur la journée (score absolu, H2H X sur Y)
# ──────────────────────────────────────────────────────────────────────────────

def detect_alltime_records(conn, gw_info: dict) -> list[dict]:
    """Détecte si un match de cette journée a battu/égalé un record all-time."""
    display = _load_display_names()
    div_id = gw_info["division_id"]
    gw = gw_info["game_week"]
    events: list[dict] = []

    # Tous les matchs de toutes les divisions (hors COVID)
    all_divs = list_included_divisions(conn, include_current=True)
    ph = ",".join("?" * len(all_divs))
    all_matches = conn.execute(
        f"SELECT m.*, dm.season FROM matches m "
        f"JOIN divisions_metadata dm ON m.division_id = dm.division_id "
        f"WHERE m.division_id IN ({ph}) AND m.is_finalized=1",
        all_divs,
    ).fetchall()

    pid_map_global: dict[str, str] = {}
    for d in all_divs:
        pid_map_global.update(_team_pid_map(conn, d))

    # Calcul des records all-time AVANT cette journée (matchs antérieurs)
    def _is_before_gw(m) -> bool:
        if m["division_id"] != div_id:
            return True  # autre division : forcément avant ou ailleurs
        return m["game_week"] < gw

    prev_matches = [m for m in all_matches if _is_before_gw(m)]
    cur_gw_matches = [m for m in all_matches
                      if m["division_id"] == div_id and m["game_week"] == gw]

    # ── Plus large écart absolu jamais observé ──
    def _abs_gap(m):
        return abs((m["home_score"] or 0) - (m["away_score"] or 0))

    prev_max_gap = max((_abs_gap(m) for m in prev_matches), default=0)
    for m in cur_gw_matches:
        gap = _abs_gap(m)
        if gap > prev_max_gap and gap > 0:
            hp = pid_map_global.get(m["home_team_id"])
            ap = pid_map_global.get(m["away_team_id"])
            if not hp or not ap:
                continue
            winner_pid = hp if (m["home_score"] or 0) > (m["away_score"] or 0) else ap
            loser_pid = ap if winner_pid == hp else hp
            ws = max(m["home_score"] or 0, m["away_score"] or 0)
            ls = min(m["home_score"] or 0, m["away_score"] or 0)
            events.append({
                "type":   "record_alltime_gap",
                "winner": _player_card(winner_pid, display),
                "loser":  _player_card(loser_pid, display),
                "score":  f"{int(ws)}-{int(ls)}",
                "gap":    int(gap),
                "old_gap": int(prev_max_gap),
            })
            prev_max_gap = gap  # un seul record par journée même si plusieurs matchs

    # ── Plus large H2H X sur Y all-time ──
    # Pour chaque match de la journée, regarder la plus grosse marge X→Y dans l'historique
    h2h_max: dict[tuple[str, str], int] = defaultdict(int)
    for m in prev_matches:
        hp = pid_map_global.get(m["home_team_id"])
        ap = pid_map_global.get(m["away_team_id"])
        if not hp or not ap:
            continue
        hs, as_ = m["home_score"] or 0, m["away_score"] or 0
        if hs > as_:
            margin = hs - as_
            if margin > h2h_max[(hp, ap)]:
                h2h_max[(hp, ap)] = int(margin)
        elif as_ > hs:
            margin = as_ - hs
            if margin > h2h_max[(ap, hp)]:
                h2h_max[(ap, hp)] = int(margin)

    for m in cur_gw_matches:
        hp = pid_map_global.get(m["home_team_id"])
        ap = pid_map_global.get(m["away_team_id"])
        if not hp or not ap:
            continue
        hs, as_ = m["home_score"] or 0, m["away_score"] or 0
        if hs == as_:
            continue
        if hs > as_:
            winner_pid, loser_pid, margin = hp, ap, hs - as_
        else:
            winner_pid, loser_pid, margin = ap, hp, as_ - hs
        old = h2h_max.get((winner_pid, loser_pid), 0)
        if margin > old and old > 0:
            events.append({
                "type":     "record_h2h",
                "winner":   _player_card(winner_pid, display),
                "loser":    _player_card(loser_pid, display),
                "score":    f"{int(max(hs, as_))}-{int(min(hs, as_))}",
                "margin":   int(margin),
                "old_margin": int(old),
            })

    return events


# ──────────────────────────────────────────────────────────────────────────────
# 3. Scores extrêmes de la journée
# ──────────────────────────────────────────────────────────────────────────────

def detect_gw_extreme_scores(conn, gw_info: dict) -> list[dict]:
    """Plus large écart de la journée, plus serré, total max."""
    display = _load_display_names()
    div_id = gw_info["division_id"]
    gw = gw_info["game_week"]
    pid_map = _team_pid_map(conn, div_id)
    matches = _gw_matches(conn, div_id, gw)
    if not matches:
        return []

    events: list[dict] = []
    # Plus large écart de la journée
    def _gap(m): return abs((m["home_score"] or 0) - (m["away_score"] or 0))
    largest = max(matches, key=_gap)
    if _gap(largest) >= 3:
        hp = pid_map.get(largest["home_team_id"])
        ap = pid_map.get(largest["away_team_id"])
        if hp and ap:
            hs, as_ = largest["home_score"] or 0, largest["away_score"] or 0
            winner = hp if hs > as_ else ap
            loser  = ap if winner == hp else hp
            events.append({
                "type":   "gw_largest_gap",
                "winner": _player_card(winner, display),
                "loser":  _player_card(loser, display),
                "score":  f"{int(max(hs, as_))}-{int(min(hs, as_))}",
                "gap":    int(_gap(largest)),
            })

    # Total max de buts
    def _total(m): return (m["home_score"] or 0) + (m["away_score"] or 0)
    most_goals = max(matches, key=_total)
    if _total(most_goals) >= 6:
        hp = pid_map.get(most_goals["home_team_id"])
        ap = pid_map.get(most_goals["away_team_id"])
        if hp and ap:
            hs, as_ = most_goals["home_score"] or 0, most_goals["away_score"] or 0
            events.append({
                "type":     "gw_most_goals",
                "home":     _player_card(hp, display),
                "away":     _player_card(ap, display),
                "score":    f"{int(hs)}-{int(as_)}",
                "total":    int(_total(most_goals)),
            })

    return events


# ──────────────────────────────────────────────────────────────────────────────
# 4. Records de série (réutilise compute_streaks → notable_events)
# ──────────────────────────────────────────────────────────────────────────────

def detect_streak_records(conn) -> list[dict]:
    """Réutilise compute_streaks pour détecter égalisations / dépassements."""
    display = _load_display_names()
    live = compute_streaks(conn, include_current=True)
    events: list[dict] = []

    cat_meta = [
        ("win",      "victoires consécutives"),
        ("unbeaten", "matchs sans défaite"),
        ("loss",     "défaites consécutives"),
    ]
    for pid in PLAYER_ORDER:
        s = live.get(pid)
        if not s:
            continue
        for cat, label in cat_meta:
            if cat == "win":
                cur_len = s["current_length"] if s["current_type"] == "W" else 0
                cur_start = s["current_start"]
            elif cat == "loss":
                cur_len = s["current_length"] if s["current_type"] == "L" else 0
                cur_start = s["current_start"]
            else:
                cur_len = s["current_unbeaten_length"]
                cur_start = s["current_unbeaten_start"]
            if cur_len == 0:
                continue
            excl = s[f"best_{cat}_excl"]
            if excl <= 0:
                continue
            if cur_len > excl:
                kind = "surpassed"
            elif cur_len == excl:
                kind = "equaled"
            elif cur_len == excl - 1:
                kind = "approaching"
            else:
                continue
            events.append({
                "type":        "streak_record",
                "kind":        kind,
                "category":    cat,
                "label":       label,
                **_player_card(pid, display),
                "value":       cur_len,
                "old_record":  excl,
                "since":       f"J{cur_start['game_week']} {slabel(cur_start['division_id'])}" if cur_start else "",
            })
    return events


# ──────────────────────────────────────────────────────────────────────────────
# 5. Bonus décisifs (qui ont changé le résultat W/D/L)
# ──────────────────────────────────────────────────────────────────────────────

# Bonus dont on sait simuler le contrefactuel proprement
SIMULATABLE_BONUSES = {
    "boostOnePlayer", "boostAllPlayers", "nerfGoalkeeper", "nerfAllPlayers",
    "removeGoal", "mirror",
}


def _outcome(g_for: float, g_against: float) -> str:
    if g_for > g_against:
        return "W"
    if g_for < g_against:
        return "L"
    return "D"


def detect_decisive_bonuses(conn, gw_info: dict) -> list[dict]:
    """Bonus dont le retrait change le résultat (W↔D↔L) ou avec gros impact (>3 buts)."""
    display = _load_display_names()
    div_id = gw_info["division_id"]
    gw = gw_info["game_week"]
    pid_map = _team_pid_map(conn, div_id)

    matches = conn.execute(
        "SELECT raw_json, home_team_id, away_team_id, home_score, away_score, "
        "home_bonuses, away_bonuses FROM matches "
        "WHERE division_id=? AND game_week=? AND is_finalized=1",
        (div_id, gw),
    ).fetchall()

    events: list[dict] = []
    for m in matches:
        if not m["raw_json"]:
            continue
        try:
            data = json.loads(m["raw_json"])
        except (json.JSONDecodeError, TypeError):
            continue

        # Score réel (vérité terrain)
        hs_real = m["home_score"] or 0
        as_real = m["away_score"] or 0
        actual_outcome_home = _outcome(hs_real, as_real)

        for side, team_id_key in (("home", "home_team_id"), ("away", "away_team_id")):
            opp_side = "away" if side == "home" else "home"
            team_pid = pid_map.get(m[team_id_key])
            opp_pid = pid_map.get(m["away_team_id" if side == "home" else "home_team_id"])
            if not team_pid or not opp_pid:
                continue
            bonuses = data.get(side, {}).get("bonuses", {}) or {}
            for bt in bonuses:
                if bt not in SIMULATABLE_BONUSES:
                    continue
                try:
                    sim_without = simulate_without_bonus(data, side, bt)
                except Exception:
                    continue
                if side == "home":
                    g_for_sim = sim_without.home.total_goals
                    g_against_sim = sim_without.away.total_goals
                    g_for_real = hs_real
                    g_against_real = as_real
                    outcome_real = actual_outcome_home
                else:
                    g_for_sim = sim_without.away.total_goals
                    g_against_sim = sim_without.home.total_goals
                    g_for_real = as_real
                    g_against_real = hs_real
                    outcome_real = (
                        "W" if hs_real < as_real
                        else "L" if hs_real > as_real else "D"
                    )
                outcome_sim = _outcome(g_for_sim, g_against_sim)
                if outcome_sim == outcome_real:
                    continue  # pas décisif
                events.append({
                    "type":         "decisive_bonus",
                    "bonus_type":   bt,
                    "user":         _player_card(team_pid, display),
                    "opponent":     _player_card(opp_pid, display),
                    "score_real":   f"{int(g_for_real)}-{int(g_against_real)}",
                    "score_sim":    f"{int(round(g_for_sim))}-{int(round(g_against_sim))}",
                    "outcome_real": outcome_real,
                    "outcome_sim":  outcome_sim,
                })
    return events


# ──────────────────────────────────────────────────────────────────────────────
# Aggregator
# ──────────────────────────────────────────────────────────────────────────────

def detect_all_events(conn) -> Optional[dict]:
    """Détecte tous les événements de la dernière journée terminée.

    Retourne None si pas de journée finalisée.
    """
    gw_info = find_last_finalized_gw(conn)
    if not gw_info:
        return None

    events: list[dict] = []
    events.extend(detect_season_status(conn, gw_info))
    events.extend(detect_alltime_records(conn, gw_info))
    events.extend(detect_gw_extreme_scores(conn, gw_info))
    events.extend(detect_streak_records(conn))
    events.extend(detect_decisive_bonuses(conn, gw_info))

    return {
        "season":       gw_info["season"],
        "division_id":  gw_info["division_id"],
        "game_week":    gw_info["game_week"],
        "slabel":       gw_info["slabel"],
        "events":       events,
    }


if __name__ == "__main__":
    import pprint
    with get_conn() as _c:
        result = detect_all_events(_c)
    pprint.pprint(result)
