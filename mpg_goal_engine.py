"""MPG Goal Engine — Reconstruction du système de buts virtuels.

Logique de simulation (mode classique, notes MPG) :

  Buts réels : player.goals > 0 dans le raw_json (vrais buts Ligue 1).
  Buts virtuels : simulation ligne par ligne pour chaque joueur éligible.

  Eligibilité but virtuel :
    - note effective (rating + bonusRating) >= 5.0
    - n'a PAS marqué de but réel (goals == 0)
    - n'est PAS gardien (position != 1)
    - max 1 but MPG par joueur

  Parcours par poste :
    pos=4 ATT  →  DEF adverse → GK adverse
    pos=3 MID  →  MID adverse → DEF adverse → GK adverse
    pos=2 DEF  →  FWD adverse → MID adverse → DEF adverse → GK adverse

  Pénalités de note :
    1ère ligne franchie : -1.0 pt
    Lignes suivantes    : -0.5 pt chacune

  Condition pour franchir une ligne :
    note_courante > moy_ligne       (si extérieur)
    note_courante >= moy_ligne      (si domicile — avantage en cas d'égalité)

  Bonus déjà intégrés dans bonusRating des joueurs (nerfGoalkeeper, boostAll…).
  removeGoal géré en post-processing (annule le but d'un joueur ciblé).
  removeGoal avec isCanceled=True (Miroir adverse) → ignoré.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional


# ── Constantes ────────────────────────────────────────────────────────────────

POSITION_GK  = 1
POSITION_DEF = 2
POSITION_MID = 3
POSITION_FWD = 4

# Lignes à traverser par poste (dans l'ordre)
LINES_BY_POS: dict[int, list[str]] = {
    POSITION_FWD: ["def", "gk"],
    POSITION_MID: ["mid", "def", "gk"],
    POSITION_DEF: ["fwd", "mid", "def", "gk"],
}


# ── Structures de données ──────────────────────────────────────────────────────

@dataclass
class PlayerSlot:
    slot: int                 # 1-11 = titulaire, 12+ = banc
    player_id: str
    position: int             # 1=GK 2=DEF 3=MID 4=FWD
    rating: float             # note brute (0-10)
    bonus_rating: float       # bonus appliqué (peut être négatif)
    goals_real: int           # vrais buts Ligue 1 (inclut canceledGoal)
    mpg_goals: int = 0        # buts virtuels officiels MPG (mpgGoals du JSON)
    last_name: str = ""
    is_sub: Optional[str] = None   # None | "mandatory" | "tactical"

    @property
    def effective_rating(self) -> float:
        return self.rating + self.bonus_rating

    @property
    def eligible_for_virtual(self) -> bool:
        return (
            self.goals_real == 0
            and self.position != POSITION_GK
            and self.effective_rating >= 5.0
        )


@dataclass
class LineAverages:
    gk:  float = 5.0
    fwd: float = 5.0
    mid: float = 5.0
    def_: float = 5.0   # 'def' est un mot-clé Python

    def get(self, line: str) -> float:
        return {"gk": self.gk, "fwd": self.fwd, "mid": self.mid, "def": self.def_}[line]


@dataclass
class TeamSimResult:
    real_goals: int = 0
    virtual_goals: int = 0
    own_goals: int = 0                                    # Fix 1 : CSC des starters adverses
    virtual_scorers: list[str] = field(default_factory=list)
    virtual_scorer_pids: set = field(default_factory=set)  # Fix 4 : lookup par player_id
    remove_goal_applied: bool = False
    remove_goal_target: Optional[str] = None

    @property
    def total_goals(self) -> int:
        return self.real_goals + self.virtual_goals + self.own_goals


@dataclass
class MatchSimResult:
    home: TeamSimResult = field(default_factory=TeamSimResult)
    away: TeamSimResult = field(default_factory=TeamSimResult)
    home_score_actual: Optional[float] = None
    away_score_actual: Optional[float] = None

    @property
    def matches_actual(self) -> bool:
        return (
            self.home_score_actual is not None
            and self.away_score_actual is not None
            and self.home.total_goals == int(self.home_score_actual)
            and self.away.total_goals == int(self.away_score_actual)
        )

    @property
    def goal_diff(self) -> int:
        return self.home.total_goals - self.away.total_goals


# ── Parsing ────────────────────────────────────────────────────────────────────

def _parse_team_starters(team_data: dict) -> list[PlayerSlot]:
    """Extrait les 11 titulaires (slots 1-11) avec leurs notes effectives."""
    starters: list[PlayerSlot] = []
    players_map = team_data.get("players", {})
    pitch = team_data.get("playersOnPitch", {})

    for slot_str, info in pitch.items():
        slot = int(slot_str)
        if slot > 11:
            continue
        pid = info.get("playerId")
        if not pid:
            continue
        p = players_map.get(pid, {})
        rating = p.get("rating")
        if rating is None:
            continue  # joueur sans note (absent)

        starters.append(PlayerSlot(
            slot=slot,
            player_id=pid,
            position=p.get("position", 0),
            rating=float(rating),
            bonus_rating=float(p.get("bonusRating") or 0),
            goals_real=int(p.get("goals") or 0) + int(p.get("canceledGoal") or 0),
            mpg_goals=int(p.get("mpgGoals") or 0),
            last_name=p.get("lastName", ""),
            is_sub=info.get("isSub"),
        ))

    return sorted(starters, key=lambda s: s.slot)


def _compute_line_averages(starters: list[PlayerSlot]) -> LineAverages:
    """Moyenne par ligne des notes effectives."""
    by_pos: dict[int, list[float]] = defaultdict(list)
    for p in starters:
        by_pos[p.position].append(p.effective_rating)

    def avg(pos: int) -> float:
        vals = by_pos.get(pos, [])
        return sum(vals) / len(vals) if vals else 5.0

    return LineAverages(
        gk=by_pos[POSITION_GK][0] if by_pos.get(POSITION_GK) else 5.0,
        def_=avg(POSITION_DEF),
        mid=avg(POSITION_MID),
        fwd=avg(POSITION_FWD),
    )


def _parse_remove_goal(bonuses: dict) -> tuple[Optional[str], bool]:
    """Retourne (player_id_cible, is_canceled) pour removeGoal, ou (None, False)."""
    rg = bonuses.get("removeGoal")
    if not rg:
        return None, False
    return rg.get("playerId"), bool(rg.get("isCanceled", False))


def _count_own_goals(data: dict, side: str) -> int:
    """Compte les own goals des starters adverses (slots 1-11) qui bénéficient à 'side'."""
    opp = "away" if side == "home" else "home"
    og = 0
    for slot_str, info in data[opp].get("playersOnPitch", {}).items():
        if int(slot_str) > 11:
            continue
        pid = info.get("playerId")
        if pid:
            og += int(data[opp]["players"].get(pid, {}).get("ownGoals") or 0)
    return og


# ── Simulation ─────────────────────────────────────────────────────────────────

def _can_pass_line(note: float, line_avg: float, is_home: bool) -> bool:
    """Vérifie si le joueur franchit la ligne (avantage domicile en cas d'égalité)."""
    return note >= line_avg if is_home else note > line_avg


def _simulate_virtual_goal(player: PlayerSlot, opp_avgs: LineAverages, is_home: bool) -> bool:
    """Retourne True si le joueur marque un but virtuel."""
    if not player.eligible_for_virtual:
        return False
    lines = LINES_BY_POS.get(player.position, [])
    note = player.effective_rating
    penalty = 0.0
    for i, line in enumerate(lines):
        pen = 1.0 if i == 0 else 0.5
        avg = opp_avgs.get(line)
        if not _can_pass_line(note - penalty, avg, is_home):
            return False
        penalty += pen
    return True


def _simulate_team_goals(
    att_starters: list[PlayerSlot],
    opp_avgs: LineAverages,
    is_home: bool,
    use_mpg_goals: bool = True,
) -> TeamSimResult:
    """Simule les buts d'une équipe (réels + virtuels).

    Own goals et removeGoal sont gérés séparément dans simulate_match.

    use_mpg_goals=True  → utilise mpgGoals du JSON (buts virtuels officiels MPG)
    use_mpg_goals=False → formule probabiliste (pour simulations contrefactuelles)
    """
    result = TeamSimResult()

    for p in att_starters:
        if p.goals_real > 0:
            result.real_goals += p.goals_real
        if use_mpg_goals:
            if p.mpg_goals > 0:
                result.virtual_goals += p.mpg_goals
                result.virtual_scorer_pids.add(p.player_id)
                result.virtual_scorers.append(p.last_name or p.player_id)
        else:
            if _simulate_virtual_goal(p, opp_avgs, is_home):
                result.virtual_goals += 1
                result.virtual_scorer_pids.add(p.player_id)
                result.virtual_scorers.append(p.last_name or p.player_id)

    return result


def simulate_match(raw_json_str: str, use_mpg_goals: bool = True) -> MatchSimResult:
    """Simule un match complet depuis son raw_json.

    Retourne un MatchSimResult avec les buts réels + virtuels calculés,
    et les scores stockés en DB pour comparaison.
    """
    data = json.loads(raw_json_str) if isinstance(raw_json_str, str) else raw_json_str

    home_data = data.get("home", {})
    away_data = data.get("away", {})

    h_starters = _parse_team_starters(home_data)
    a_starters = _parse_team_starters(away_data)

    if not h_starters or not a_starters:
        return MatchSimResult(
            home_score_actual=data["home"].get("score"),
            away_score_actual=data["away"].get("score"),
        )

    h_avgs = _compute_line_averages(h_starters)
    a_avgs = _compute_line_averages(a_starters)

    h_bonuses = home_data.get("bonuses", {})
    a_bonuses = away_data.get("bonuses", {})

    # removeGoal : l'équipe qui l'utilise annule un but ADVERSE
    h_rg_pid, h_rg_canceled = _parse_remove_goal(h_bonuses)
    a_rg_pid, a_rg_canceled = _parse_remove_goal(a_bonuses)

    home_result = _simulate_team_goals(h_starters, a_avgs, is_home=True,  use_mpg_goals=use_mpg_goals)
    away_result = _simulate_team_goals(a_starters, h_avgs, is_home=False, use_mpg_goals=use_mpg_goals)

    # Fix 1 : own goals (CSC des starters adverses bénéficient à cette équipe)
    home_result.own_goals = _count_own_goals(data, "home")
    away_result.own_goals = _count_own_goals(data, "away")

    # Appliquer removeGoal sur l'équipe cible (home annule un but de away, et vice-versa)
    if h_rg_pid and not h_rg_canceled:
        _apply_remove_goal(away_result, a_starters, h_rg_pid)
    if a_rg_pid and not a_rg_canceled:
        _apply_remove_goal(home_result, h_starters, a_rg_pid)

    # Fix 2 : Mirror reflected removeGoal
    # Quand Mirror s'active, il réfléchit le removeGoal sur l'équipe adverse
    h_mirror = h_bonuses.get("mirror", {})
    a_mirror = a_bonuses.get("mirror", {})
    if "removeGoal" in h_mirror:
        reflected_pid = h_mirror["removeGoal"].get("playerId")
        if reflected_pid:
            _apply_remove_goal(away_result, a_starters, reflected_pid)
    if "removeGoal" in a_mirror:
        reflected_pid = a_mirror["removeGoal"].get("playerId")
        if reflected_pid:
            _apply_remove_goal(home_result, h_starters, reflected_pid)

    result = MatchSimResult(
        home=home_result,
        away=away_result,
        home_score_actual=data["home"].get("score"),
        away_score_actual=data["away"].get("score"),
    )
    return result


def _apply_remove_goal(
    team_result: TeamSimResult,
    starters: list[PlayerSlot],
    target_pid: str,
) -> None:
    """Modifie team_result en place : annule le but du joueur ciblé.

    Fix 3 : si le joueur est introuvable ou n'a pas de but, ne rien faire.
    Fix 4 : lookup des buts virtuels par player_id (plus par nom).
    """
    team_result.remove_goal_applied = True
    team_result.remove_goal_target = target_pid

    # But réel du joueur ciblé ?
    for p in starters:
        if p.player_id == target_pid:
            if p.goals_real > 0:
                team_result.real_goals = max(0, team_result.real_goals - 1)
            return  # Joueur trouvé dans les starters → on s'arrête ici

    # But virtuel du joueur ciblé ? (Fix 4 : lookup par player_id)
    if target_pid in team_result.virtual_scorer_pids:
        team_result.virtual_goals = max(0, team_result.virtual_goals - 1)
        team_result.virtual_scorer_pids.discard(target_pid)
    # Fix 3 : pas de fallback — si introuvable, ne rien faire


# ── Simulation contrefactuelle (impact des bonus) ──────────────────────────────

def simulate_without_bonus(
    raw_json_str: str,
    side: str,
    bonus_type: str,
) -> MatchSimResult:
    """Rejoue le match comme si le bonus_type du côté 'side' n'avait pas été utilisé.

    Supporte : boostOnePlayer, boostAllPlayers, nerfGoalkeeper, nerfAllPlayers,
               removeGoal, blockTacticalSubs (approximation), fourStrikers (TODO).

    Args:
        raw_json_str: raw_json du match
        side: 'home' ou 'away'
        bonus_type: clé du bonus (ex: 'boostOnePlayer')

    Returns:
        MatchSimResult simulé sans le bonus
    """
    data = json.loads(raw_json_str) if isinstance(raw_json_str, str) else raw_json_str
    import copy
    data = copy.deepcopy(data)

    team = data[side]
    opp_side = "away" if side == "home" else "home"
    opp = data[opp_side]
    bonuses = team.get("bonuses", {})

    if bonus_type not in bonuses:
        return simulate_match(data)

    bonus_info = bonuses[bonus_type]

    # ── boostOnePlayer (+1 à 1 joueur de son équipe) ──
    if bonus_type == "boostOnePlayer":
        target_pid = bonus_info.get("playerId")
        for p in team["players"].values():
            if p.get("playerId") == target_pid:
                p["bonusRating"] = (p.get("bonusRating") or 0) - 1.0
                break

    # ── boostAllPlayers / nerfAllPlayers (±0.5 à tous les joueurs de champ) ──
    elif bonus_type in ("boostAllPlayers", "nerfAllPlayers"):
        delta = bonus_info.get("bonusRating", 0.5)
        for p in team["players"].values():
            if p.get("position", 0) != POSITION_GK:
                p["bonusRating"] = (p.get("bonusRating") or 0) - delta

    # ── nerfGoalkeeper (-1 au GK adverse) ──
    elif bonus_type == "nerfGoalkeeper":
        delta = bonus_info.get("bonusRating", -1.0)
        # Trouver le GK adverse
        gk_slot = opp["playersOnPitch"].get("1", {})
        gk_pid = gk_slot.get("playerId")
        if gk_pid and gk_pid in opp["players"]:
            opp["players"][gk_pid]["bonusRating"] = (
                opp["players"][gk_pid].get("bonusRating") or 0
            ) - delta  # enlever le nerf (delta est négatif donc on soustrait un négatif)

    # ── removeGoal (annule un but adverse) ──
    elif bonus_type == "removeGoal":
        # Supprimer le bonus — la simulation n'appliquera plus le removeGoal
        del team["bonuses"]["removeGoal"]

    # ── blockTacticalSubs (approximation : on revert les subs tactiques) ──
    elif bonus_type == "blockTacticalSubs":
        # Note : approximation — on ne peut pas reconstruire exactement
        # l'équipe adverse sans les subs tactiques sans données additionnelles
        pass

    # ── mirror (annule le removeGoal adverse + réfléchit) ──
    elif bonus_type == "mirror":
        del team["bonuses"]["mirror"]
        # Réactiver le removeGoal adverse s'il a été annulé par ce Mirror
        opp_bonuses = opp.get("bonuses", {})
        if "removeGoal" in opp_bonuses:
            opp_bonuses["removeGoal"].pop("isCanceled", None)

    return simulate_match(data, use_mpg_goals=False)


# ── Analyse de l'impact des bonus ─────────────────────────────────────────────

def analyze_bonus_impact(
    conn,
    bonus_types: Optional[list[str]] = None,
    max_matches: Optional[int] = None,
) -> dict[str, dict]:
    """Analyse l'impact de chaque bonus en simulant avec et sans.

    Pour chaque match où le bonus X est utilisé :
      - Simule le résultat avec le bonus (baseline)
      - Simule sans le bonus
      - Mesure le delta de buts et de résultat

    Returns:
        dict[bonus_type → {
            n_matches, delta_goals_avg, win_rate_with, win_rate_without,
            result_changed_pct, details: [...]
        }]
    """
    if bonus_types is None:
        bonus_types = [
            "boostOnePlayer", "boostAllPlayers", "nerfGoalkeeper",
            "nerfAllPlayers", "removeGoal", "mirror",
        ]

    query = """
        SELECT raw_json, home_score, away_score, home_bonuses, away_bonuses
        FROM matches m
        JOIN divisions_metadata dm ON m.division_id = dm.division_id
        WHERE dm.is_covid=0 AND dm.is_current=0
        AND m.home_score IS NOT NULL
    """
    if max_matches:
        query += f" LIMIT {max_matches}"

    rows = conn.execute(query).fetchall()

    def _outcome(g: int, opp: int) -> str:
        """Retourne 'W', 'D' ou 'L' du point de vue de g."""
        if g > opp: return "W"
        if g < opp: return "L"
        return "D"

    results: dict[str, dict] = {bt: {
        "n": 0, "delta_goals": [],
        "w_with": 0, "d_with": 0, "l_with": 0,
        "w_without": 0, "d_without": 0, "l_without": 0,
        "result_changed": 0,
    } for bt in bonus_types}

    for row in rows:
        data = json.loads(row["raw_json"])
        for side in ("home", "away"):
            team_bonuses = data[side].get("bonuses", {})
            for bt in bonus_types:
                if bt not in team_bonuses:
                    continue
                # Avec bonus : simulation mpgGoals (vérité terrain)
                # Sans bonus : simulation probabiliste (modèle contrefactuel)
                with_result    = simulate_match(data, use_mpg_goals=True)
                without_result = simulate_without_bonus(data, side, bt)

                if side == "home":
                    goals_with    = with_result.home.total_goals
                    goals_without = without_result.home.total_goals
                    opp_with      = with_result.away.total_goals
                    opp_without   = without_result.away.total_goals
                else:
                    goals_with    = with_result.away.total_goals
                    goals_without = without_result.away.total_goals
                    opp_with      = with_result.home.total_goals
                    opp_without   = without_result.home.total_goals

                delta = (goals_with - opp_with) - (goals_without - opp_without)
                outcome_with    = _outcome(goals_with, opp_with)
                outcome_without = _outcome(goals_without, opp_without)
                result_changed  = outcome_with != outcome_without

                r = results[bt]
                r["n"] += 1
                r["delta_goals"].append(delta)
                r[f"{outcome_with.lower()}_with"]    += 1
                r[f"{outcome_without.lower()}_without"] += 1
                if result_changed: r["result_changed"] += 1

    # Agrégation
    final: dict[str, dict] = {}
    for bt, r in results.items():
        n = r["n"]
        if n == 0:
            continue
        deltas = r["delta_goals"]
        final[bt] = {
            "n_matches":          n,
            "avg_goal_delta":     sum(deltas) / n,
            "pct_positive_delta": sum(1 for d in deltas if d > 0) / n * 100,
            "win_rate_with":      r["w_with"] / n * 100,
            "draw_rate_with":     r["d_with"] / n * 100,
            "loss_rate_with":     r["l_with"] / n * 100,
            "win_rate_without":   r["w_without"] / n * 100,
            "draw_rate_without":  r["d_without"] / n * 100,
            "loss_rate_without":  r["l_without"] / n * 100,
            "result_changed_pct": r["result_changed"] / n * 100,
        }
    return final


def print_bonus_impact_report(impact: dict[str, dict]) -> None:
    """Affiche le rapport d'impact des bonus dans la console."""
    NAMES = {
        "boostOnePlayer":   "McDo+ (+1 à 1 joueur)",
        "boostAllPlayers":  "Zahia (+0.5 tous)",
        "nerfGoalkeeper":   "Suarez (-1 GK adverse)",
        "nerfAllPlayers":   "Cheat Code (-0.5 tous adv.)",
        "removeGoal":       "Valise à Nanard (annule 1 but)",
        "mirror":           "Tonton Pat' (miroir)",
        "blockTacticalSubs":"Tonton Pat'",
        "fourStrikers":     "Décathlon (4 ATT)",
    }

    print("\n" + "="*72)
    print("  IMPACT DES BONUS — simulation contrefactuelle")
    print("="*72)
    print(f"{'Bonus':<30} {'N':>5}  {'ΔButs':>7}  {'W%/avec':>8}  {'W%/sans':>8}  {'Résultat↑':>10}")
    print("-"*72)

    sorted_items = sorted(impact.items(), key=lambda x: -x[1]["avg_goal_delta"])
    for bt, r in sorted_items:
        name = NAMES.get(bt, bt)
        delta = r["avg_goal_delta"]
        sign  = "+" if delta >= 0 else ""
        print(
            f"{name:<30} {r['n_matches']:>5}  "
            f"{sign}{delta:>6.3f}  "
            f"{r['win_rate_with']:>7.1f}%  "
            f"{r['win_rate_without']:>7.1f}%  "
            f"{r['result_changed_pct']:>9.1f}%"
        )
    print("="*72)
    print("  ΔButs = impact moyen sur la diff. de buts (pos. = avantage)")
    print("  Résultat↑ = % de matchs où le bonus a changé le résultat final")
    print()


# ── Validation ─────────────────────────────────────────────────────────────────

def validate(conn, n: int = 500, verbose: bool = False) -> dict:
    """Compare les scores simulés aux scores réels sur n matchs aléatoires."""
    rows = conn.execute(f"""
        SELECT raw_json, home_score, away_score FROM matches m
        JOIN divisions_metadata dm ON m.division_id = dm.division_id
        WHERE dm.is_covid=0 AND dm.is_current=0
        AND m.home_score IS NOT NULL
        ORDER BY RANDOM() LIMIT {n}
    """).fetchall()

    exact = near = wrong = 0
    diffs: list[float] = []

    for row in rows:
        r = simulate_match(row["raw_json"])
        if r.home_score_actual is None:
            continue
        dh = abs(r.home.total_goals - int(r.home_score_actual))
        da = abs(r.away.total_goals - int(r.away_score_actual))
        diffs.append(dh + da)
        if dh == 0 and da == 0:
            exact += 1
        elif dh <= 1 and da <= 1:
            near += 1
        else:
            wrong += 1
            if verbose:
                print(f"  WRONG calc={r.home.total_goals}-{r.away.total_goals} "
                      f"actual={int(r.home_score_actual)}-{int(r.away_score_actual)}")

    total = exact + near + wrong
    return {
        "total": total,
        "exact": exact,
        "near": near,
        "wrong": wrong,
        "exact_pct": exact / total * 100 if total else 0,
        "near_pct": near / total * 100 if total else 0,
        "avg_diff": sum(diffs) / len(diffs) if diffs else 0,
    }


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    sys.path.insert(0, ".")
    from mpg_db import get_conn

    conn = get_conn()

    print("Validation du moteur sur 500 matchs...")
    v = validate(conn, n=500, verbose=False)
    print(f"  Exact  : {v['exact']:>4} / {v['total']}  ({v['exact_pct']:.1f}%)")
    print(f"  ±1 but : {v['near']:>4} / {v['total']}  ({v['near_pct']:.1f}%)")
    print(f"  Wrong  : {v['wrong']:>4} / {v['total']}")
    print(f"  Diff moy: {v['avg_diff']:.3f} buts/match")

    print("\nAnalyse de l'impact des bonus sur 1008 matchs...")
    impact = analyze_bonus_impact(conn)
    print_bonus_impact_report(impact)
