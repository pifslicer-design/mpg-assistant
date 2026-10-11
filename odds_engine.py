#!/usr/bin/env python3
"""odds_engine.py — Cotes champion / chapeau d'une saison MPG : simulateur Monte Carlo + backtest.

Principe
--------
1. Modèle joueur (`PlayerModel`) : pour chaque joueur des 8 effectifs, une note ~ N(mu, sigma)
   arrondie au 0,5 et bornée 1-9, une probabilité d'être aligné `p_play`, un taux de buts réels
   (Poisson plafonné à 3, par match joué) et un décalage de note les jours de but. Les paramètres
   mélangent la saison L1 en cours (ratings_<l1_season>.json) et la saison de référence
   (ratings_<ref_season>.json) par un prior hiérarchique : niveau de la saison de référence du
   joueur rétréci vers son poste (PRIOR_K_REF pseudo-matchs), puis mis à jour par les matchs de la
   saison en cours avec un poids de prior PRIOR_W (≈ 7 matchs) — PRIOR_W_NEW (3) seulement pour un
   joueur sans saison de référence (nouveau en L1 : prior de poste seul). Un bon joueur 2025 peu
   aligné en 2026 garde donc l'essentiel de son niveau, et une disponibilité intermédiaire :
   « retour progressif » en espérance, sans rampe temporelle (p_play constant sur la saison
   simulée). Sans saison de référence du tout (backtest), le prior est la moyenne du poste.
2. Compo attendue : le XI par poste est choisi par la valeur attendue
   v = p_play·(mu + GOAL_VALUE·goal_rate) + (1 − p_play)·4,5 ; la FORMATION est choisie par
   simulation (`choose_formations`) : chaque formation jouable est testée sur des matchs
   domicile/extérieur contre la compo courante des 7 autres managers et on garde celle qui
   maximise les points attendus (deux passes de meilleure réponse). Le critère Σ v + bonus défense
   de la spec initiale (= bestteam_engine.best_formation) reste disponible (`lineup="value"`) mais
   pousse tout le monde en 5-4-1 alors que les managers réels jouent 4-3-3 / 3-4-3 à > 90 %.
   La compo est figée pour toute la saison simulée (aucune info blessure pour ré-optimiser).
3. Match : disponibilité de chaque titulaire, remplaçant du même poste sinon « Rotaldo » (2,5,
   compte dans la moyenne de ligne, jamais buteur), notes et buts réels tirés, bonus défense
   (4D +0,5 / 5D +1,0 — appliqué aussi à Rotaldo, pas l'ajustement manager), ajustement manager
   (ELO d'avant-saison × MANAGER_COEF, plafonné à MANAGER_CAP), puis buts virtuels selon les
   règles exactes de mpg_goal_engine (franchissement de lignes, >= à domicile, > à l'extérieur,
   pénalités −1,0 puis −0,5 ; moyennes de ligne via sum() compensé comme la référence ;
   équivalence vérifiée sur 50 000 matchs aléatoires). Capitaine et bonus consommables ignorés
   (décision validée) ; CSC non modélisés (≈ 0,1 but/match réel).
4. Saison : les 56 paires ordonnées (A dom., B ext.) non encore jouées, `n_sims` fois. Les matchs
   étant indépendants et la compo fixe, l'ordre du calendrier n'influe pas sur les résultats :
   les paires restantes sont parcourues dans un ordre fixe. Classement : points, différence,
   buts pour, puis tirage au sort. Lissage additif des probabilités (jamais 0 % ni 100 % hors
   saison terminée). Seed par défaut dérivé de division_id + nombre de journées finalisées.
5. Backtest : S18 « à l'aveugle » (effectifs S18, notes 2025 jusqu'à la J20 de L1, aucune saison
   de référence, ELO jusqu'à S17, classement de départ vide), comparé au classement réel ;
   grille de coefficients manager pour calibrer MANAGER_COEF. `compute_odds` embarque un résumé
   de ce backtest dans `model.backtest`.

numpy n'étant pas installé dans le venv, tout est en Python pur (random.Random + statistics),
boucle chaude sur des tuples pré-calculés (≈ 25 µs par match).

Usage :
    python3 odds_engine.py [--division ID] [--n 3000] [--coef 0.2] [--seed 42] [--json out.json] [--xi]
    python3 odds_engine.py --backtest [--division mpg_division_QU0SUZ6HQPB_18_1] [--cutoff-gw 20]
                           [--n 2000] [--coef-grid 0,0.1,0.2,0.3,0.5] [--json out.json]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from statistics import fmean, pstdev

import yaml

from bestteam_engine import (FORMATIONS, L1_SEASON, POS_LABEL, _load, _short_club,
                             load_player_index, load_teams, names_from_db)
from mpg_db import division_sort_key, get_conn, get_current_division
from mpg_legacy_engine import compute_mpg_season_standings, fetch_matches, list_included_divisions
from mpg_people import DEFAULT_MAPPING_PATH

# ── Paramètres du modèle ─────────────────────────────────────────────────────

MANAGER_COEF = 0.1        # abaissé à 0,1 le 2026-10-09 à la demande de Raph (à 0,2 l'ELO dominait les cotes
                          # d'avant-saison : Raph 14 → 25 % de titre). Calibration initiale sur S18 (notes 2025 ≤ J20, 2000 saisons, seed 42) :
                          # Σ log-loss titre+chapeau = 6,42 (coef 0) · 5,92 (0,1) · 5,82 (0,2) · 5,79 (0,3) · 5,71 (0,5).
                          # La règle « min sous ≤ 0,3 » donnerait 0,3, mais 0,2 fait aussi bien (écart 0,04 < 0,05)
                          # et tout le gain vient du chapeau (Pierre, ELO le plus bas) alors que la log-loss du
                          # champion se dégrade avec le coef : on garde le plus petit effet équivalent.
                          # Backtest globalement mauvais (champion réel prédit #5-6) : calibration fragile, cf. rapport.
MANAGER_CAP = 0.25        # plafond absolu de l'ajustement de note (± par joueur)
N_SIMS_DEFAULT = 3000
BACKTEST_SIMS_DEFAULT = 1000   # saisons du backtest résumé embarqué dans compute_odds (0 = désactivé)

TOTAL_GW = 14
N_TEAMS = 8
MATCHES_PER_SEASON = N_TEAMS * (N_TEAMS - 1)   # 56 paires ordonnées

PRIOR_W = 7.0             # poids du niveau de la saison de référence face aux matchs de la saison en cours
PRIOR_K_RATING = 6.0      # pseudo-matchs du poste pour rétrécir la note de la saison de référence (consigne de
                          # Raph : les perfs passées comptent, ex. Dembélé ; la régression vers la moyenne mesurée
                          # J1-20 → J21-34 de L1 2025 suggérerait plutôt k ≈ 16, sans effet notable sur le backtest)
PRIOR_K_GOALS = 8.0       # idem pour le taux de buts (pente 0,65 → k ≈ 8,5)
PRIOR_K_PLAY = 4.0        # idem pour la disponibilité de la saison de référence (consigne ; mesuré k ≈ 7)
PRIOR_W_NEW = 3.0         # joueur SANS saison de référence : poids du prior de poste pour la disponibilité
                          # (consigne : 5 titularisations sur 5 → p_play 0,83) ; la note et les buts gardent
                          # PRIOR_K_RATING / PRIOR_K_GOALS (régression vers la moyenne mesurée)
PRIOR_P_PLAY = 0.55       # disponibilité a priori d'un joueur de L1
P_PLAY_NO_DATA = 0.35     # joueur sans aucune note (ni saison en cours, ni référence)
P_PLAY_MIN, P_PLAY_MAX = 0.05, 0.98
MIN_POSS_GW = 5           # journées « possibles » minimales depuis la première apparition
SIGMA_K = 8.0             # rétrécissement de l'écart-type individuel vers celui du poste
SIGMA_MIN, SIGMA_MAX = 0.6, 1.4
MIN_PRIOR_ENTRIES = 30    # en dessous, le prior de poste est repris de la saison de référence
LEGACY_LAMBDA, LEGACY_K = 0.5, 6.0   # ancienne formule (λ, k), gardée pour comparaison (blend="legacy")

ROTALDO_RATING = 2.5
ABSENT_VALUE = 4.5        # valeur d'un titulaire absent dans le choix de la compo
GOAL_VALUE = 1.5          # poids du taux de buts dans la valeur de compo (les buts réels ≈ 70 % du score)
DEF_BONUS = {4: 0.5, 5: 1.0}   # bonus défense structurel par défenseur aligné
LINEUP_SIM_MATCHES = 60   # choix de formation : matchs dom. + ext. par adversaire et par formation
LINEUP_ROUNDS = 2         # passes de meilleure réponse
LINEUP_MAX_DEF = 4        # formations candidates : au plus 4 défenseurs (5D < 5 % des compos réelles S15-S18 ;
                          # le moteur, lui, préfère souvent 5-3-2 de +0,04 à +0,07 pt/match — cf. --max-def 5)

POS_LIST = ("G", "D", "M", "A")
RATING_BINS = 19          # histogramme des notes tirées : indice = note × 2 (1,0 → 2 … 9,0 → 18)

# Couleurs des managers (= generate_pages.PLAYER_COLORS ; copie locale : generate_pages importe ce module)
MANAGER_COLORS = {
    "raph": "#C0303A", "nico": "#2E6A99", "francois": "#1E5C3A", "damien": "#C08A00",
    "greg": "#D4643A", "marc": "#1A3D50", "pierre": "#2A8FA0", "manu": "#7A1A1E",
}
DEFAULT_COLOR = "#777777"

DEFAULT_BACKTEST_DIVISION = "mpg_division_QU0SUZ6HQPB_18_1"


# ── Structures ───────────────────────────────────────────────────────────────

@dataclass(slots=True)
class PlayerModel:
    pid: str
    name: str          # « O. Dembélé »
    pos: str           # G / D / M / A
    club: str          # shortName du club (3 lettres)
    mu: float          # note attendue (inconditionnelle)
    sigma: float
    p_play: float      # probabilité d'être aligné un jour de match
    goal_rate: float   # buts réels attendus par match joué
    mu_nogoal: float   # note moyenne les jours sans but
    mu_goal: float     # note moyenne les jours avec but
    n_cur: int         # entrées (matchs joués) de la saison en cours
    n_ref: int         # entrées de la saison de référence
    value: float       # valeur pour le choix du XI


# ── Utilitaires ──────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slabel(div_id: str) -> str:
    n = div_id.split("_")[-2]
    return f"Old S{n}" if "PWN77AILXZQ" in div_id else f"S{n}"


def _season_number(div_id: str) -> int | None:
    try:
        return int(div_id.split("_")[-2])
    except (ValueError, IndexError):
        return None


def _clip(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


def _display_names() -> dict[str, str]:
    """{person_id: display_name} depuis people_mapping.yaml (comme mpg_legacy_engine)."""
    try:
        data = yaml.safe_load(DEFAULT_MAPPING_PATH.read_text(encoding="utf-8"))
        return {pid: info.get("display_name", pid) for pid, info in (data.get("persons") or {}).items()}
    except Exception:
        return {}


def _load_ratings(season: int) -> dict:
    """ratings_<season>.json du cache mercato, avec n_gw garanti."""
    data = _load(f"ratings_{season}.json")
    if not data or not data.get("players"):
        raise RuntimeError(f"Notes L1 absentes : mercato_cache/ratings_{season}.json")
    if not data.get("n_gw"):
        data["n_gw"] = max((int(g) for g, v in (data.get("gws") or {}).items() if v.get("played")), default=0)
    return data


def _default_seed(division_id: str, played_gw: int) -> int:
    """Seed déterministe : mêmes données (division + journées finalisées) → mêmes chiffres."""
    digest = hashlib.sha256(f"{division_id}|{played_gw}".encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big")


def _played_gw(conn, division_id: str) -> int:
    return len({m["game_week"] for m in fetch_matches(conn, [division_id])})


# ── Modèle joueur ────────────────────────────────────────────────────────────

def _position_map(idx: dict, db_names: dict) -> dict[str, str]:
    """Poste de chaque joueur : index.json/pool.json → compos MPG stockées (matches.raw_json)."""
    pos: dict[str, str] = {}
    for pid, ix in idx.items():
        p = POS_LABEL.get((ix or {}).get("position"))
        if p:
            pos[pid] = p
    for pid, d in db_names.items():
        pos.setdefault(pid, (d or {}).get("pos") or "M")
    return pos


def _position_priors(entries_by_pid: dict[str, list[dict]], pos_of: dict[str, str]) -> dict[str, dict]:
    """Moyenne / écart-type des notes et taux de buts par poste, sur toutes les entrées données."""
    ratings: dict[str, list[float]] = {p: [] for p in POS_LIST}
    goals: dict[str, int] = {p: 0 for p in POS_LIST}
    for pid, es in entries_by_pid.items():
        p = pos_of.get(pid)
        if p not in ratings:
            continue
        for e in es:
            r = e.get("rating")
            if r is None:
                continue
            ratings[p].append(float(r))
            goals[p] += int(e.get("goals") or 0)
    all_r = [r for lst in ratings.values() for r in lst]
    g_mu = fmean(all_r) if all_r else 5.0
    g_sd = pstdev(all_r) if len(all_r) > 1 else 1.0
    out: dict[str, dict] = {}
    for p in POS_LIST:
        lst = ratings[p]
        n = len(lst)
        out[p] = {
            "mu": fmean(lst) if n else g_mu,
            "sigma": pstdev(lst) if n > 1 else g_sd,
            "goal_rate": goals[p] / n if n else 0.0,
            "n": n,
        }
    return out


def _goal_delta(entries_by_pid: dict[str, list[dict]]) -> float:
    """Décalage moyen intra-joueur de la note les jours de but (note avec but − moyenne du joueur)."""
    deltas: list[float] = []
    for es in entries_by_pid.values():
        rated = [e for e in es if e.get("rating") is not None]
        if len(rated) < 3:
            continue
        m = fmean(float(e["rating"]) for e in rated)
        deltas.extend(float(e["rating"]) - m for e in rated if int(e.get("goals") or 0) > 0)
    return fmean(deltas) if deltas else 1.4


def _possible_gws(entries: list[dict], n_gw: int) -> int:
    """Journées « possibles » : de la première apparition du joueur à la coupure (min MIN_POSS_GW)."""
    if not entries:
        return n_gw
    first = min(int(e.get("gw") or 1) for e in entries)
    return max(MIN_POSS_GW, n_gw - first + 1)


def _identity(pid: str, idx: dict, db_names: dict, clubs: dict,
              cur_entries: list[dict], ref_entries: list[dict]) -> tuple[str, str]:
    """(nom court « O. Dembélé », club 3 lettres) d'un joueur."""
    ix = idx.get(pid) or {}
    dbn = db_names.get(pid) or {}
    first = (ix.get("firstName") or dbn.get("first") or "").strip()
    last = (ix.get("lastName") or dbn.get("last") or "").strip()
    if first and last:
        name = f"{first[0]}. {last}"
    else:
        name = last or first or pid.replace("mpg_championship_player_", "joueur ")
    club_id = None
    if cur_entries:
        club_id = max(cur_entries, key=lambda e: int(e.get("gw") or 0)).get("club")
    club_id = club_id or ix.get("clubId")
    if not club_id and ref_entries:
        club_id = max(ref_entries, key=lambda e: int(e.get("gw") or 0)).get("club")
    club_id = club_id or dbn.get("club_id")
    return name, (_short_club(clubs, club_id) if club_id else "")


def fit_player_models(pids: list[str], cur: dict, ref: dict | None, cutoff_gw: int | None,
                      pos_of: dict[str, str], idx: dict, db_names: dict, clubs: dict,
                      blend: str = "hier", log=print) -> tuple[dict[str, PlayerModel], dict]:
    """Ajuste un PlayerModel par joueur.

    blend="hier" (défaut) : prior hiérarchique — niveau saison de référence du joueur rétréci vers
    le poste (K = PRIOR_K_RATING pour la note, PRIOR_K_GOALS pour les buts, PRIOR_K_PLAY pour la
    disponibilité), puis mise à jour par la saison en cours avec le poids W = PRIOR_W :
        mu_ref = (Σ notes réf + K·mu_poste) / (n_ref + K)      mu = (Σ notes cour + W·mu_ref) / (n_cour + W)
        idem taux de buts ; p_play_ref = (joués_ref + 4·0,55) / (poss_ref + 4),
        p_play = (joués_cour + W·p_play_ref) / (poss_cour + W).
    Sans saison de référence (nouveau en L1, ou backtest) : prior de poste seul, mu et buts avec les
    mêmes K, disponibilité avec le poids PRIOR_W_NEW (3).
    blend="legacy" : formule initiale mu = (Σ cour + λ·Σ réf + k·mu_poste) / (n + λ·n_ref + k), λ = 0,5, k = 6.
    Journées possibles : de la première apparition du joueur à la coupure, min 5 (saison en cours
    comme saison de référence). sigma : écart-type individuel (toutes saisons) rétréci vers celui
    du poste (SIGMA_K), borné [0,6 ; 1,4]. Joueur sans aucune note : prior de poste, p_play 0,35.
    Les jours de but la note est tirée autour de mu_goal = mu_nogoal + delta_goal, avec mu_nogoal
    choisi pour que l'espérance inconditionnelle reste mu.
    Retourne (models, infos : priors de poste, delta_goal, n_gw).
    """
    cur_players = cur.get("players") or {}
    n_gw_cur = int(cur["n_gw"]) if cutoff_gw is None else min(int(cur["n_gw"]), int(cutoff_gw))
    cur_entries = {
        pid: [e for e in es if cutoff_gw is None or int(e.get("gw") or 0) <= cutoff_gw]
        for pid, es in cur_players.items()
    }
    ref_entries: dict[str, list[dict]] = (ref or {}).get("players") or {}
    n_gw_ref = int((ref or {}).get("n_gw") or 0)

    priors = _position_priors(cur_entries, pos_of)
    if ref_entries:
        ref_priors = _position_priors(ref_entries, pos_of)
        for p in POS_LIST:
            if priors[p]["n"] < MIN_PRIOR_ENTRIES:
                priors[p] = dict(ref_priors[p], fallback=True)
    delta_goal = _goal_delta(ref_entries if ref_entries else cur_entries)
    log("  priors de poste (saison en cours" + (f", repli réf. si < {MIN_PRIOR_ENTRIES} entrées" if ref_entries else "") + ") :")
    for p in POS_LIST:
        pr = priors[p]
        log(f"    {p} : note {pr['mu']:.2f} ± {pr['sigma']:.2f}, buts/match {pr['goal_rate']:.3f}, n={pr['n']}"
            + (" (réf.)" if pr.get("fallback") else ""))
    log(f"  décalage de note les jours de but : +{delta_goal:.2f} ; mélange « {blend} » "
        f"(W={PRIOR_W:g}, K note {PRIOR_K_RATING:g} / buts {PRIOR_K_GOALS:g} / dispo {PRIOR_K_PLAY:g}, "
        f"dispo sans réf. {PRIOR_W_NEW:g})")

    models: dict[str, PlayerModel] = {}
    for pid in pids:
        pos = pos_of.get(pid, "M")
        pr = priors[pos]
        ce = [e for e in cur_entries.get(pid) or [] if e.get("rating") is not None]
        re_ = [e for e in ref_entries.get(pid) or [] if e.get("rating") is not None]
        n_c, n_r = len(ce), len(re_)
        s_c = sum(float(e["rating"]) for e in ce)
        s_r = sum(float(e["rating"]) for e in re_)
        g_c = sum(int(e.get("goals") or 0) for e in ce)
        g_r = sum(int(e.get("goals") or 0) for e in re_)
        poss_c = _possible_gws(ce, n_gw_cur)
        poss_r = _possible_gws(re_, n_gw_ref) if n_r else 0

        if blend == "legacy":
            lam, k = LEGACY_LAMBDA, LEGACY_K
            mu = (s_c + lam * s_r + k * pr["mu"]) / (n_c + lam * n_r + k)
            gr = (g_c + lam * g_r + k * pr["goal_rate"]) / (n_c + lam * n_r + k)
            pp = (n_c + lam * n_r + PRIOR_K_PLAY * PRIOR_P_PLAY) / (poss_c + lam * poss_r + PRIOR_K_PLAY)
        elif n_r > 0:       # vrai niveau de saison de référence : rétréci vers le poste, puis mis à jour (poids W)
            mu_ref = (s_r + PRIOR_K_RATING * pr["mu"]) / (n_r + PRIOR_K_RATING)
            gr_ref = (g_r + PRIOR_K_GOALS * pr["goal_rate"]) / (n_r + PRIOR_K_GOALS)
            pp_ref = (n_r + PRIOR_K_PLAY * PRIOR_P_PLAY) / (poss_r + PRIOR_K_PLAY)
            mu = (s_c + PRIOR_W * mu_ref) / (n_c + PRIOR_W)
            gr = (g_c + PRIOR_W * gr_ref) / (n_c + PRIOR_W)
            pp = (n_c + PRIOR_W * pp_ref) / (poss_c + PRIOR_W)
        else:               # sans saison de référence (nouveau en L1, ou backtest) : prior de poste seul
            mu = (s_c + PRIOR_K_RATING * pr["mu"]) / (n_c + PRIOR_K_RATING)
            gr = (g_c + PRIOR_K_GOALS * pr["goal_rate"]) / (n_c + PRIOR_K_GOALS)
            pp = (n_c + PRIOR_W_NEW * PRIOR_P_PLAY) / (poss_c + PRIOR_W_NEW)
        if n_c == 0 and n_r == 0:
            pp = P_PLAY_NO_DATA
        pp = _clip(pp, P_PLAY_MIN, P_PLAY_MAX)
        gr = max(0.0, gr)

        all_r = [float(e["rating"]) for e in ce] + [float(e["rating"]) for e in re_]
        n_tot = len(all_r)
        s_j = pstdev(all_r) if n_tot >= 2 else pr["sigma"]
        sigma = _clip((n_tot * s_j + SIGMA_K * pr["sigma"]) / (n_tot + SIGMA_K), SIGMA_MIN, SIGMA_MAX)

        p_goal = 1.0 - math.exp(-gr)              # P(au moins un but) sous Poisson
        mu_nogoal = mu - p_goal * delta_goal
        name, club = _identity(pid, idx, db_names, clubs, ce, re_)
        models[pid] = PlayerModel(
            pid=pid, name=name, pos=pos, club=club, mu=mu, sigma=sigma, p_play=pp, goal_rate=gr,
            mu_nogoal=mu_nogoal, mu_goal=mu_nogoal + delta_goal, n_cur=n_c, n_ref=n_r,
            value=pp * (mu + GOAL_VALUE * gr) + (1.0 - pp) * ABSENT_VALUE,
        )
    info = {"priors": priors, "delta_goal": delta_goal, "n_gw_cur": n_gw_cur, "n_gw_ref": n_gw_ref,
            "blend": blend}
    return models, info


# ── Compo attendue ───────────────────────────────────────────────────────────

def lineup_options(models: list[PlayerModel], max_def: int = LINEUP_MAX_DEF) -> dict[str, dict]:
    """Pour chaque formation jouable : {nom: {"starters", "bench", "value"}} (meilleurs v par poste).

    Formations à plus de max_def défenseurs exclues ; seules celles laissant le moins de postes vides
    sont gardées (effectif incomplet → les postes manquants seront des Rotaldo permanents).
    "value" = Σ v + bonus défense structurel.
    """
    by_pos = {p: sorted((m for m in models if m.pos == p), key=lambda m: -m.value) for p in POS_LIST}
    opts: dict[str, dict] = {}
    for name, slots in FORMATIONS.items():
        if slots["D"] > max_def:
            continue
        missing, total = 0, 0.0
        for p, cnt in slots.items():
            picked = by_pos[p][:cnt]
            missing += cnt - len(picked)
            total += sum(m.value for m in picked)
        total += slots["D"] * DEF_BONUS.get(slots["D"], 0.0)
        opts[name] = {"starters": {p: by_pos[p][:slots[p]] for p in POS_LIST},
                      "bench": {p: by_pos[p][slots[p]:] for p in POS_LIST},
                      "value": total, "missing": missing}
    min_missing = min(o["missing"] for o in opts.values())
    return {k: o for k, o in opts.items() if o["missing"] == min_missing}


def pick_lineup(models: list[PlayerModel], max_def: int = LINEUP_MAX_DEF
                ) -> tuple[str, dict[str, list[PlayerModel]], dict[str, list[PlayerModel]]]:
    """Formation maximisant Σ valeurs + bonus défense (règle de la spec initiale = bestteam_engine.best_formation)."""
    opts = lineup_options(models, max_def)
    name = max(opts, key=lambda k: opts[k]["value"])
    return name, opts[name]["starters"], opts[name]["bench"]


# ── Effet manager (ELO d'avant-saison) ───────────────────────────────────────

def elo_before(conn, division_id: str, k: int = 20) -> dict[str, float]:
    """ELO (K=20, base 1500) calculé sur les seules divisions strictement antérieures à division_id
    (ordre mpg_db.division_sort_key, divisions hors COVID / incomplètes). Non arrondi."""
    key = division_sort_key(division_id)
    divs = [d for d in list_included_divisions(conn) if division_sort_key(d) < key]
    ratings: dict[str, float] = defaultdict(lambda: 1500.0)
    for m in fetch_matches(conn, divs):
        hp, ap = m["home_person_id"], m["away_person_id"]
        if not hp or not ap:
            continue
        r_h, r_a = ratings[hp], ratings[ap]
        exp_h = 1.0 / (1.0 + 10.0 ** ((r_a - r_h) / 400.0))
        fr = m["final_result"]
        score_h = 1.0 if fr == 1 else 0.5 if fr == 2 else 0.0
        ratings[hp] = r_h + k * (score_h - exp_h)
        ratings[ap] = r_a + k * ((1.0 - score_h) - (1.0 - exp_h))
    return dict(ratings)


def manager_adjustment(elo: float, manager_coef: float) -> float:
    return _clip(manager_coef * (elo - 1500.0) / 100.0, -MANAGER_CAP, MANAGER_CAP)


# ── Boucle chaude (tuples pré-calculés) ──────────────────────────────────────

def _player_tuple(m: PlayerModel) -> tuple:
    """(mu_nogoal, mu_goal, sigma, p_play, P(0 but), P(≤1), P(≤2)) — seuils cumulés de Poisson."""
    lam = m.goal_rate
    e = math.exp(-lam)
    p0 = e
    p1 = p0 + lam * e
    p2 = p1 + lam * lam * 0.5 * e
    return (m.mu_nogoal, m.mu_goal, m.sigma, m.p_play, p0, p1, p2)


def _team_tuple(t: dict, manager_coef: float) -> tuple:
    """Équipe prête pour la simulation : (gardiens, banc G, ((tit., banc, besoin, bonus déf.) × D/M/A), adj)."""
    adj = manager_adjustment(t["elo"], manager_coef)
    slots = FORMATIONS[t["formation"]]
    dbonus = DEF_BONUS.get(slots["D"], 0.0)
    st, bn = t["starters"], t["bench"]

    def tp(lst):
        return [_player_tuple(m) for m in lst]

    return (tp(st["G"]), tp(bn["G"]),
            ((tp(st["D"]), tp(bn["D"]), slots["D"], dbonus),
             (tp(st["M"]), tp(bn["M"]), slots["M"], 0.0),
             (tp(st["A"]), tp(bn["A"]), slots["A"], 0.0)),
            adj)


def _make_draw_team(rng: random.Random, hist: list[int], rot: list[int]):
    """Fabrique draw_team(team) lié à un générateur.

    draw_team tire la compo du jour : disponibilité des titulaires, remplaçants du même poste,
    Rotaldo pour les trous ; notes (N(mu, sigma) arrondies au 0,5, bornées 1-9) et buts réels
    (Poisson plafonné à 3) ; bonus défense + ajustement manager. Retourne
    (note gardien, [(moyenne de ligne, [notes effectives des non-buteurs])] pour D/M/A, buts réels).
    Les moyennes de ligne utilisent sum() (compensé en Python ≥ 3.12) comme mpg_goal_engine.
    hist[note×2] et rot[0] (slots Rotaldo) sont incrémentés pour les statistiques.
    """
    rnd = rng.random
    gauss = rng.gauss

    def draw_team(team):
        gk_s, gk_b, lines_def, adj = team
        goals = 0
        p = None
        for c in gk_s:
            if rnd() < c[3]:
                p = c
                break
        if p is None:
            for c in gk_b:
                if rnd() < c[3]:
                    p = c
                    break
        if p is None:
            gk = ROTALDO_RATING
            rot[0] += 1
            hist[5] += 1
        else:
            x = gauss(p[0], p[2])
            if x < 1.0:
                x = 1.0
            elif x > 9.0:
                x = 9.0
            k = int(x * 2.0 + 0.5)
            hist[k] += 1
            gk = k * 0.5 + adj
        lines = []
        for st, bn, need, dbonus in lines_def:
            avail = [c for c in st if rnd() < c[3]]
            missing = need - len(avail)
            if missing > 0:
                for c in bn:
                    if rnd() < c[3]:
                        avail.append(c)
                        missing -= 1
                        if missing == 0:
                            break
            bonus = dbonus + adj
            effs = []
            cands = []
            for c in avail:
                u = rnd()
                if u < c[4]:
                    g = 0
                elif u < c[5]:
                    g = 1
                elif u < c[6]:
                    g = 2
                else:
                    g = 3
                x = gauss(c[1] if g else c[0], c[2])
                if x < 1.0:
                    x = 1.0
                elif x > 9.0:
                    x = 9.0
                k = int(x * 2.0 + 0.5)
                hist[k] += 1
                eff = k * 0.5 + bonus
                effs.append(eff)
                if g:
                    goals += g
                else:
                    cands.append(eff)
            if missing > 0:
                effs.extend([ROTALDO_RATING + dbonus] * missing)
                rot[0] += missing
                hist[5] += missing
            lines.append((sum(effs) / need, cands))
        return gk, lines, goals

    return draw_team


def _virtual_goals(lines_att, gk_opp, lines_opp, is_home: bool) -> int:
    """Buts virtuels d'une équipe — réimplémentation plate de mpg_goal_engine._simulate_virtual_goal.

    lines_att : [(moy. ligne, [notes effectives des non-buteurs])] pour D, M, A de l'attaque ;
    gk_opp / lines_opp : note du gardien et lignes adverses. Mêmes règles que le moteur de référence :
    éligible si note >= 5 (et pas de but réel), lignes à franchir A→[déf, gard.], M→[mil, déf, gard.],
    D→[att, mil, déf, gard.], pénalités cumulées 0 / 1,0 / 1,5 / 2,0, >= à domicile, > à l'extérieur
    (équivalence vérifiée sur 50 000 matchs aléatoires contre le moteur de référence).
    """
    d_c, m_c, a_c = lines_att[0][1], lines_att[1][1], lines_att[2][1]
    od, om, of = lines_opp[0][0], lines_opp[1][0], lines_opp[2][0]
    og = gk_opp
    v = 0
    if is_home:
        for e in a_c:
            if e >= 5.0 and e >= od and e - 1.0 >= og:
                v += 1
        for e in m_c:
            if e >= 5.0 and e >= om and e - 1.0 >= od and e - 1.5 >= og:
                v += 1
        for e in d_c:
            if e >= 5.0 and e >= of and e - 1.0 >= om and e - 1.5 >= od and e - 2.0 >= og:
                v += 1
    else:
        for e in a_c:
            if e >= 5.0 and e > od and e - 1.0 > og:
                v += 1
        for e in m_c:
            if e >= 5.0 and e > om and e - 1.0 > od and e - 1.5 > og:
                v += 1
        for e in d_c:
            if e >= 5.0 and e > of and e - 1.0 > om and e - 1.5 > od and e - 2.0 > og:
                v += 1
    return v


# ── Choix de formation par simulation ────────────────────────────────────────

def choose_formations(teams: list[dict], rng: random.Random, k: int = LINEUP_SIM_MATCHES,
                      rounds: int = LINEUP_ROUNDS, max_def: int = LINEUP_MAX_DEF, log=print) -> None:
    """Formation de chaque manager par meilleure réponse simulée (modifie teams en place).

    Pour chaque formation jouable (XI = meilleurs v par poste, cf. lineup_options), k matchs à
    domicile et k à l'extérieur contre la compo courante de chacun des 7 autres managers, sans
    effet manager ; on retient la formation qui maximise les points moyens (égalité → Σ v). Les
    tirages adverses sont communs à toutes les formations testées d'un même manager (générateur
    adverse remis au même état), ce qui réduit fortement la variance de la comparaison. Deux passes
    (LINEUP_ROUNDS) suffisent en pratique ; on s'arrête dès qu'une passe ne change rien.
    """
    n = len(teams)
    hist_dummy = [0] * RATING_BINS
    rot_dummy = [0]
    rng_opp = random.Random(rng.random())
    rng_own = random.Random(rng.random())
    draw_opp = _make_draw_team(rng_opp, hist_dummy, rot_dummy)
    draw_own = _make_draw_team(rng_own, hist_dummy, rot_dummy)
    vgoals = _virtual_goals
    options = [lineup_options(t["players"], max_def) for t in teams]
    T = [_team_tuple(t, 0.0) for t in teams]
    for rnd_i in range(rounds):
        changed = 0
        for i, t in enumerate(teams):
            opts = options[i]
            if len(opts) <= 1:
                continue
            opp_state = rng_opp.getstate()
            scores: dict[str, float] = {}
            for name, o in opts.items():
                rng_opp.setstate(opp_state)
                Ti = _team_tuple({"elo": 1500.0, "formation": name, "starters": o["starters"], "bench": o["bench"]}, 0.0)
                pts = 0
                for j in range(n):
                    if j == i:
                        continue
                    Tj = T[j]
                    for _ in range(k):
                        gk_j, lj, g_j = draw_opp(Tj)
                        gk_i, li, g_i = draw_own(Ti)
                        si = g_i + vgoals(li, gk_j, lj, True)
                        sj = g_j + vgoals(lj, gk_i, li, False)
                        pts += 3 if si > sj else (1 if si == sj else 0)
                        gk_j, lj, g_j = draw_opp(Tj)
                        gk_i, li, g_i = draw_own(Ti)
                        si = g_i + vgoals(li, gk_j, lj, False)
                        sj = g_j + vgoals(lj, gk_i, li, True)
                        pts += 3 if si > sj else (1 if si == sj else 0)
                scores[name] = pts / (2.0 * k * (n - 1))
            best = max(scores, key=lambda nm: (scores[nm], opts[nm]["value"]))
            t["lineup_scores"] = {nm: round(s, 3) for nm, s in scores.items()}
            if best != t["formation"]:
                changed += 1
                t["formation"] = best
                t["starters"] = opts[best]["starters"]
                t["bench"] = opts[best]["bench"]
                T[i] = _team_tuple(t, 0.0)
        log(f"  choix des formations par simulation, passe {rnd_i + 1} : {changed} changement(s)")
        if changed == 0:
            break


# ── Préparation d'une division ───────────────────────────────────────────────

def _played_state(conn, division_id: str, index_of: dict[str, int], ignore_played: bool) -> dict:
    """Classement de départ et paires ordonnées restantes à partir des matchs finalisés."""
    n = len(index_of)
    st = {k: [0] * n for k in ("pts", "gd", "gf", "ga", "w", "d", "l", "played")}
    played_pairs: set[tuple[int, int]] = set()
    gws: set[int] = set()
    n_played = 0
    real_matches = []
    if not ignore_played:
        for m in fetch_matches(conn, [division_id]):
            hi, ai = index_of.get(m["home_person_id"]), index_of.get(m["away_person_id"])
            if hi is None or ai is None:
                continue
            hs, as_ = int(round(m["home_score"])), int(round(m["away_score"]))
            played_pairs.add((hi, ai))
            gws.add(m["game_week"])
            n_played += 1
            real_matches.append((hs, as_))
            for i, gf, ga in ((hi, hs, as_), (ai, as_, hs)):
                st["gf"][i] += gf
                st["ga"][i] += ga
                st["gd"][i] += gf - ga
                st["played"][i] += 1
                if gf > ga:
                    st["w"][i] += 1
                    st["pts"][i] += 3
                elif gf < ga:
                    st["l"][i] += 1
                else:
                    st["d"][i] += 1
                    st["pts"][i] += 1
    fixtures = [(i, j) for i in range(n) for j in range(n) if i != j and (i, j) not in played_pairs]
    st.update({"fixtures": fixtures, "n_played": n_played, "played_gw": len(gws), "real_matches": real_matches})
    return st


def _finalize_team(t: dict) -> None:
    """XI dans l'ordre G, D, M, A, force (moyenne des mu du XI) et Σ goal_rate du XI."""
    xi = [m for p in POS_LIST for m in t["starters"][p]]
    t["xi"] = xi
    t["strength"] = fmean(m.mu for m in xi) if xi else 0.0
    t["xi_goal_rate"] = sum(m.goal_rate for m in xi)
    t["xi_p_play"] = fmean(m.p_play for m in xi) if xi else 0.0


def _prepare(conn, division_id: str, l1_season: int, ref_season: int | None, cutoff_gw: int | None,
             ignore_played: bool, blend: str, lineup: str, seed: int, log=print,
             max_def: int = LINEUP_MAX_DEF) -> dict:
    """Charge effectifs, identités, notes, ELO et classement de départ ; ajuste modèles et compos."""
    teams = load_teams(conn, division_id)
    teams = [t for t in teams if t.get("person_id")]
    if not teams:
        raise RuntimeError(f"Aucune équipe en base pour {division_id}")
    teams.sort(key=lambda t: t["person_id"])          # ordre stable → seed reproductible
    display = _display_names()
    idx, _stats = load_player_index()
    clubs = _load("clubs.json", {}) or {}
    db_names = names_from_db(conn, division_id)
    cur = _load_ratings(l1_season)
    ref = None
    if ref_season:
        try:
            ref = _load_ratings(ref_season)
        except RuntimeError as exc:
            log(f"  ⚠ {exc} — pas de saison de référence")
    pos_of = _position_map(idx, db_names)
    all_pids = [pid for t in teams for pid in t["squad"]]
    missing_pos = [pid for pid in all_pids if pid not in pos_of]
    if missing_pos:
        log(f"  ⚠ {len(missing_pos)} joueur(s) sans poste connu → milieu par défaut")
    log(f"  {_slabel(division_id)} : {len(teams)} équipes, {len(all_pids)} joueurs ; notes L1 {l1_season}"
        f" (J1-J{cur['n_gw'] if cutoff_gw is None else min(cur['n_gw'], cutoff_gw)})"
        + (f", référence {ref_season} ({ref['n_gw']} journées)" if ref else ", sans saison de référence"))
    models, fit_info = fit_player_models(all_pids, cur, ref, cutoff_gw, pos_of, idx, db_names, clubs, blend, log)
    elo = elo_before(conn, division_id)
    index_of = {t["person_id"]: i for i, t in enumerate(teams)}
    state = _played_state(conn, division_id, index_of, ignore_played)

    team_ctx = []
    for t in teams:
        pid = t["person_id"]
        pms = [models[p] for p in t["squad"]]
        formation, starters, bench = pick_lineup(pms, max_def)      # point de départ : règle Σ v (spec)
        team_ctx.append({
            "pid": pid, "name": display.get(pid, pid), "team_name": t["name"],
            "elo": elo.get(pid, 1500.0), "squad_size": len(t["squad"]), "players": pms,
            "formation": formation, "starters": starters, "bench": bench, "lineup_scores": None,
        })
    if lineup == "sim":
        choose_formations(team_ctx, random.Random(seed + 1), max_def=max_def, log=log)
    for t in team_ctx:
        _finalize_team(t)
        log(f"    {t['name']:<9} {t['formation']}  force {t['strength']:.2f}  ELO {t['elo']:6.1f}"
            f"  effectif {t['squad_size']:>2}  XI p_play {t['xi_p_play']:.2f}  Σ buts/match {t['xi_goal_rate']:.2f}")
    return {
        "division_id": division_id, "teams": team_ctx, "models": models, "fit": fit_info, "state": state,
        "lineup": lineup, "max_def": max_def,
        "ratings": {"l1_season": l1_season, "n_gw": fit_info["n_gw_cur"], "cutoff_gw": cutoff_gw,
                    "ref_season": ref_season if ref else None, "ref_cutoff_gw": None,
                    "blend": blend, "prior_w": PRIOR_W, "prior_k_rating": PRIOR_K_RATING,
                    "prior_k_goals": PRIOR_K_GOALS, "prior_k_play": PRIOR_K_PLAY, "prior_w_new": PRIOR_W_NEW},
    }


# ── Simulation de saison ─────────────────────────────────────────────────────

def _simulate(ctx: dict, n_sims: int, seed: int, manager_coef: float) -> dict:
    """Boucle chaude : n_sims saisons sur les paires restantes. Retourne compteurs bruts + sim_stats."""
    rng = random.Random(seed)
    rnd = rng.random
    hist = [0] * RATING_BINS
    rot = [0]
    draw_team = _make_draw_team(rng, hist, rot)
    vgoals = _virtual_goals
    teams = ctx["teams"]
    n = len(teams)
    state = ctx["state"]
    fixtures = state["fixtures"]
    T = [_team_tuple(t, manager_coef) for t in teams]

    start_pts, start_gd, start_gf = state["pts"], state["gd"], state["gf"]
    titles = [0] * n
    podiums = [0] * n
    chapeaux = [0] * n
    rank_counts = [[0] * n for _ in range(n)]
    sum_pts = [0] * n
    sum_rank = [0] * n
    sum_gf = [0] * n
    sum_gd = [0] * n
    tot_real = tot_virt = n_draw = n_home = n_away = 0
    idx_range = range(n)

    for _ in range(n_sims):
        pts = start_pts[:]
        gd = start_gd[:]
        gf = start_gf[:]
        for h, a in fixtures:
            gk_h, lh, g_h = draw_team(T[h])
            gk_a, la, g_a = draw_team(T[a])
            v_h = vgoals(lh, gk_a, la, True)
            v_a = vgoals(la, gk_h, lh, False)
            sh = g_h + v_h
            sa = g_a + v_a
            gf[h] += sh
            gf[a] += sa
            gd[h] += sh - sa
            gd[a] += sa - sh
            tot_real += g_h + g_a
            tot_virt += v_h + v_a
            if sh > sa:
                pts[h] += 3
                n_home += 1
            elif sh < sa:
                pts[a] += 3
                n_away += 1
            else:
                pts[h] += 1
                pts[a] += 1
                n_draw += 1
        order = sorted(idx_range, key=lambda i: (-pts[i], -gd[i], -gf[i], rnd()))
        for r, i in enumerate(order):
            rank_counts[i][r] += 1
            sum_rank[i] += r + 1
            sum_pts[i] += pts[i]
            sum_gf[i] += gf[i]
            sum_gd[i] += gd[i]
        titles[order[0]] += 1
        for i in order[:3]:
            podiums[i] += 1
        chapeaux[order[-1]] += 1

    n_m = n_sims * len(fixtures)
    if n_m:
        n_slots = sum(hist) or 1
        sim_stats = {
            "avg_goals_per_match": round((tot_real + tot_virt) / n_m, 2),
            "real_goals_per_match": round(tot_real / n_m, 2),
            "virtual_goals_per_match": round(tot_virt / n_m, 2),
            "draw_rate": round(n_draw / n_m, 3),
            "home_win_rate": round(n_home / n_m, 3),
            "away_win_rate": round(n_away / n_m, 3),
            "rotaldo_rate": round(rot[0] / n_slots, 4),
            "starter_rating_mean": round(sum(k * 0.5 * c for k, c in enumerate(hist)) / n_slots, 3),
            "rating_dist": {f"{k * 0.5:g}": round(100.0 * c / n_slots, 1) for k, c in enumerate(hist) if c},
            "n_matches": n_m,
        }
    else:   # saison terminée : rien à simuler, stats des matchs réels pour information
        rm = state["real_matches"]
        k = len(rm) or 1
        sim_stats = {
            "avg_goals_per_match": round(sum(h + a for h, a in rm) / k, 2),
            "real_goals_per_match": None, "virtual_goals_per_match": None,
            "draw_rate": round(sum(1 for h, a in rm if h == a) / k, 3),
            "home_win_rate": round(sum(1 for h, a in rm if h > a) / k, 3),
            "away_win_rate": round(sum(1 for h, a in rm if h < a) / k, 3),
            "rotaldo_rate": None, "starter_rating_mean": None, "rating_dist": {},
            "n_matches": 0,
        }
    return {"titles": titles, "podiums": podiums, "chapeaux": chapeaux, "rank_counts": rank_counts,
            "sum_pts": sum_pts, "sum_rank": sum_rank, "sum_gf": sum_gf, "sum_gd": sum_gd,
            "sim_stats": sim_stats, "n_sims": n_sims}


# ── Assemblage du dictionnaire de sortie (contrat cotes.html) ────────────────

def _run(ctx: dict, n_sims: int, seed: int, manager_coef: float) -> dict:
    t0 = time.perf_counter()
    sim = _simulate(ctx, n_sims, seed, manager_coef)
    state = ctx["state"]
    teams = ctx["teams"]
    n = len(teams)
    fixtures = state["fixtures"]
    done = not fixtures
    status = "done" if done else ("pre" if state["n_played"] == 0 else "live")

    def smooth(cnt: int, add: float) -> float:
        return cnt / n_sims if done else (cnt + add) / (n_sims + 4)

    def odd(p: float) -> float | None:
        return round(1.0 / p, 1) if p > 0 else None

    cur_order = sorted(range(n), key=lambda i: (-state["pts"][i], -state["gd"][i], -state["gf"][i]))
    cur_rank = {i: r + 1 for r, i in enumerate(cur_order)}

    managers = []
    for i, t in enumerate(teams):
        p_title = round(smooth(sim["titles"][i], 0.5), 4)
        p_podium = round(smooth(sim["podiums"][i], 1.5), 4)
        p_chapeau = round(smooth(sim["chapeaux"][i], 0.5), 4)
        managers.append({
            "pid": t["pid"], "name": t["name"], "color": MANAGER_COLORS.get(t["pid"], DEFAULT_COLOR),
            "team_name": t["team_name"],
            "p_title": p_title, "p_podium": p_podium, "p_chapeau": p_chapeau,
            "odd_title": odd(p_title), "odd_chapeau": odd(p_chapeau),
            "exp_pts": round(sim["sum_pts"][i] / n_sims, 1), "exp_rank": round(sim["sum_rank"][i] / n_sims, 2),
            "exp_gf": round(sim["sum_gf"][i] / n_sims, 1),
            "exp_ga": round((sim["sum_gf"][i] - sim["sum_gd"][i]) / n_sims, 1),
            "rank_dist": [round(smooth(c, 0.5), 4) for c in sim["rank_counts"][i]],
            "current_pts": state["pts"][i], "current_rank": None if status == "pre" else cur_rank[i],
            "played": state["played"][i], "wins": state["w"][i], "draws": state["d"][i], "losses": state["l"][i],
            "gf": state["gf"][i], "ga": state["ga"][i],
            "elo": round(t["elo"], 1), "squad_size": t["squad_size"], "strength": round(t["strength"], 2),
            "formation": t["formation"],
            "xi": [{"pid": m.pid, "name": m.name, "pos": m.pos, "club": m.club,
                    "exp_rating": round(m.mu, 2), "p_play": round(m.p_play, 3),
                    "goal_rate": round(m.goal_rate, 3), "n_obs": m.n_cur, "n_ref": m.n_ref}
                   for m in t["xi"]],
            "bench": {p: len(t["bench"][p]) for p in POS_LIST},
            "xi_goal_rate": round(t["xi_goal_rate"], 2),
            "lineup_scores": t.get("lineup_scores"),
        })
    managers.sort(key=lambda m: (-m["p_title"], -m["exp_pts"]))

    fit = ctx["fit"]
    return {
        "division_id": ctx["division_id"],
        "label": _slabel(ctx["division_id"]),
        "season": _season_number(ctx["division_id"]),
        "generated_at": _now_iso(),
        "n_sims": n_sims,
        "seed": seed,
        "status": status,
        "played_gw": state["played_gw"],
        "total_gw": TOTAL_GW,
        "remaining_matches": len(fixtures),
        "calendar_source": "aléatoire" if len(fixtures) == MATCHES_PER_SEASON else "paires restantes",
        "runtime_s": round(time.perf_counter() - t0, 1),
        "model": {
            "manager_coef": manager_coef,
            "manager_cap": MANAGER_CAP,
            "ratings": dict(ctx["ratings"]),
            "lineup": ctx["lineup"],
            "lineup_max_def": ctx["max_def"],
            "goal_value": GOAL_VALUE,
            "priors": {p: {k: (round(v, 3) if isinstance(v, float) else v) for k, v in fit["priors"][p].items()}
                       for p in POS_LIST},
            "delta_goal": round(fit["delta_goal"], 3),
            "sim_stats": sim["sim_stats"],
            "backtest": None,
        },
        "managers": managers,
    }


# ── API publique ─────────────────────────────────────────────────────────────

def compute_odds(conn, division_id: str | None = None, n_sims: int = N_SIMS_DEFAULT,
                 seed: int | None = None, manager_coef: float | None = None, log=print,
                 l1_season: int = L1_SEASON, ref_season: int | None = None, blend: str = "hier",
                 lineup: str = "sim", backtest_sims: int = BACKTEST_SIMS_DEFAULT,
                 max_def: int = LINEUP_MAX_DEF) -> dict:
    """Cotes de la saison (dictionnaire du contrat cotes.html, sans `history` ni `intro`).

    Lecture seule. Division courante par défaut ; saison de référence = l1_season − 1 (ratings_2025
    pour 2026). Seed par défaut déterministe (division + journées finalisées). Si backtest_sims > 0,
    un backtest S18 (même coef, seed 42) est rejoué et résumé dans `model.backtest`.
    """
    t0 = time.perf_counter()
    if division_id is None:
        division_id = get_current_division()[0]
    coef = MANAGER_COEF if manager_coef is None else float(manager_coef)
    if ref_season is None:
        ref_season = l1_season - 1
    if seed is None:
        seed = _default_seed(division_id, _played_gw(conn, division_id))
    ctx = _prepare(conn, division_id, l1_season, ref_season, None, False, blend, lineup, seed, log, max_def)
    log(f"  simulation de {n_sims} saisons (seed {seed}, coef manager {coef:g}, "
        f"{len(ctx['state']['fixtures'])} matchs restants)…")
    res = _run(ctx, n_sims, seed, coef)
    ss = res["model"]["sim_stats"]
    log(f"  sim_stats : {ss['avg_goals_per_match']} buts/match (réels {ss['real_goals_per_match']}, "
        f"virtuels {ss['virtual_goals_per_match']}), nuls {ss['draw_rate']:.1%}, dom. {ss['home_win_rate']:.1%}, "
        f"ext. {ss['away_win_rate']:.1%}, Rotaldo {ss['rotaldo_rate']:.1%}, note tit. {ss['starter_rating_mean']}")
    if backtest_sims and division_id != DEFAULT_BACKTEST_DIVISION:
        try:
            log(f"  backtest {_slabel(DEFAULT_BACKTEST_DIVISION)} embarqué ({backtest_sims} saisons)…")
            bt = backtest(conn, n_sims=backtest_sims, seed=42, manager_coef=coef, log=lambda *_a, **_k: None,
                          blend=blend, lineup=lineup, max_def=max_def)
            res["model"]["backtest"] = backtest_summary(bt)
            m = bt["metrics"]
            log(f"    champion réel prédit #{m['champion_rank_pred']}, chapeau #{m['chapeau_rank_pred']}, "
                f"Spearman {m['spearman_exp_pts']:.2f}, LL titre {m['logloss_title']:.2f} / chapeau {m['logloss_chapeau']:.2f}")
        except Exception as exc:      # le backtest n'est qu'informatif : ne jamais bloquer les cotes
            log(f"  ⚠ backtest embarqué impossible : {exc}")
    res["runtime_s"] = round(time.perf_counter() - t0, 1)
    log(f"  cotes {res['label']} calculées en {res['runtime_s']} s")
    return res


# ── Backtest ─────────────────────────────────────────────────────────────────

def _rankdata(xs: list[float]) -> list[float]:
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    ranks = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def _pearson(a: list[float], b: list[float]) -> float:
    ma, mb = fmean(a), fmean(b)
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((y - mb) ** 2 for y in b)
    return cov / math.sqrt(va * vb) if va > 0 and vb > 0 else 0.0


def _spearman(a: list[float], b: list[float]) -> float:
    return _pearson(_rankdata(a), _rankdata(b))


def backtest_metrics(result: dict, real_rows: list[dict]) -> dict:
    """Métriques d'un résultat simulé face au classement réel (lignes de compute_mpg_season_standings)."""
    managers = result["managers"]                       # déjà triés par p_title décroissant
    by_pid = {m["pid"]: m for m in managers}
    real = [r for r in real_rows if r["person_id"] in by_pid]
    real_rank = {r["person_id"]: i + 1 for i, r in enumerate(real)}
    champion, chapeau = real[0]["person_id"], real[-1]["person_id"]
    title_order = [m["pid"] for m in managers]
    chap_order = [m["pid"] for m in sorted(managers, key=lambda m: -m["p_chapeau"])]
    pids = [r["person_id"] for r in real]
    exp_pts = [by_pid[p]["exp_pts"] for p in pids]
    real_pts = [float(r["points"]) for r in real]
    brier = []
    for p in pids:
        rd = by_pid[p]["rank_dist"]
        brier.append(sum((rd[k] - (1.0 if real_rank[p] == k + 1 else 0.0)) ** 2 for k in range(len(rd))))
    return {
        "champion_rank_pred": title_order.index(champion) + 1,
        "chapeau_rank_pred": chap_order.index(chapeau) + 1,
        "logloss_title": round(-math.log(max(by_pid[champion]["p_title"], 1e-9)), 3),
        "logloss_chapeau": round(-math.log(max(by_pid[chapeau]["p_chapeau"], 1e-9)), 3),
        "logloss_naive": round(math.log(len(pids)), 3),
        "spearman_exp_pts": round(_spearman(exp_pts, real_pts), 3),
        "mae_pts": round(fmean(abs(e - r) for e, r in zip(exp_pts, real_pts)), 2),
        "brier_rank": round(fmean(brier), 3),
    }


def choose_coef(grid: list[dict], max_coef: float = 0.3, tol: float = 0.05) -> float:
    """Coef minimisant logloss_title + logloss_chapeau sous coef ≤ max_coef ; 0 si à moins de tol."""
    cands = [g for g in grid if g["coef"] <= max_coef + 1e-9] or list(grid)

    def score(g):
        return g["logloss_title"] + g["logloss_chapeau"]

    best = min(cands, key=score)
    zero = next((g for g in cands if g["coef"] == 0), None)
    if zero is not None and score(zero) - score(best) < tol:
        return 0.0
    return best["coef"]


def backtest(conn, division_id: str = DEFAULT_BACKTEST_DIVISION, ratings_season: int = 2025,
             cutoff_gw: int = 20, n_sims: int = 2000, seed: int = 42,
             manager_coef: float | None = None, log=print,
             coef_grid: list[float] | None = None, blend: str = "hier", lineup: str = "sim",
             max_def: int = LINEUP_MAX_DEF) -> dict:
    """Backtest aveugle d'une saison terminée.

    Effectifs de la division, notes ratings_<ratings_season>.json filtrées gw <= cutoff_gw comme
    « saison en cours », AUCUNE saison de référence (prior = moyenne du poste, poids PRIOR_W_NEW),
    ELO des divisions antérieures, classement de départ vide (matchs réels ignorés). Avec coef_grid,
    simule chaque coefficient (même seed) et recommande celui de choose_coef ; la simulation finale
    utilise manager_coef s'il est donné, sinon le coef recommandé, sinon MANAGER_COEF.
    """
    t0 = time.perf_counter()
    ctx = _prepare(conn, division_id, ratings_season, None, cutoff_gw, True, blend, lineup, seed, log, max_def)
    standings = compute_mpg_season_standings(conn).get(division_id)
    if not standings or not standings.get("standings"):
        raise RuntimeError(f"Pas de classement réel pour {division_id} (division incomplète ?)")
    real_rows = standings["standings"]
    display = {t["pid"]: t["name"] for t in ctx["teams"]}

    grid: list[dict] = []
    chosen = None
    if coef_grid:
        log(f"  grille de coefficients {coef_grid} ({n_sims} saisons, seed {seed}) :")
        for coef in coef_grid:
            res = _run(ctx, n_sims, seed, float(coef))
            met = backtest_metrics(res, real_rows)
            grid.append({"coef": float(coef), **met, "sim_stats": res["model"]["sim_stats"]})
            log(f"    coef {coef:<4g} champion#{met['champion_rank_pred']} chapeau#{met['chapeau_rank_pred']}"
                f"  LL titre {met['logloss_title']:.3f} LL chapeau {met['logloss_chapeau']:.3f}"
                f"  Spearman {met['spearman_exp_pts']:.3f}  MAE {met['mae_pts']:.2f}  Brier {met['brier_rank']:.3f}")
        chosen = choose_coef(grid)
        log(f"  coef recommandé : {chosen:g}")
    if manager_coef is not None:
        coef = float(manager_coef)
    elif chosen is not None:
        coef = chosen
    else:
        coef = MANAGER_COEF
    res = _run(ctx, n_sims, seed, coef)
    metrics = backtest_metrics(res, real_rows)
    return {
        "division_id": division_id, "label": _slabel(division_id), "ratings_season": ratings_season,
        "cutoff_gw": cutoff_gw, "n_sims": n_sims, "seed": seed, "manager_coef": coef,
        "recommended_coef": chosen, "grid": grid, "metrics": metrics,
        "real": [{"pid": r["person_id"], "name": display.get(r["person_id"], r["person_id"]), "rank": i + 1,
                  "points": r["points"], "goal_diff": r["goal_diff"], "goals_for": r["goals_for"]}
                 for i, r in enumerate(real_rows)],
        "pred": res["managers"], "model": res["model"],
        "runtime_s": round(time.perf_counter() - t0, 1),
    }


def backtest_summary(bt: dict) -> dict:
    """Résumé compact d'un backtest pour `model.backtest` de compute_odds (lu par cotes.html)."""
    m = bt["metrics"]
    real_by = {r["pid"]: r for r in bt["real"]}
    return {
        "label": bt["label"], "division_id": bt["division_id"], "n_sims": bt["n_sims"], "coef": bt["manager_coef"],
        "ratings_season": bt["ratings_season"], "cutoff_gw": bt["cutoff_gw"],
        "champion": bt["real"][0]["name"], "chapeau": bt["real"][-1]["name"],
        "champion_rank": m["champion_rank_pred"], "chapeau_rank": m["chapeau_rank_pred"],
        "spearman": m["spearman_exp_pts"], "logloss_title": m["logloss_title"],
        "logloss_chapeau": m["logloss_chapeau"], "logloss_naive": m["logloss_naive"],
        "mae_pts": m["mae_pts"], "brier_rank": m["brier_rank"],
        "sim_stats": bt["model"]["sim_stats"],
        "managers": [{"pid": p["pid"], "name": p["name"], "p_title": p["p_title"], "p_chapeau": p["p_chapeau"],
                      "exp_pts": p["exp_pts"], "exp_rank": p["exp_rank"], "formation": p["formation"],
                      "real_pts": real_by.get(p["pid"], {}).get("points"),
                      "real_rank": real_by.get(p["pid"], {}).get("rank")}
                     for p in bt["pred"]],
    }


# ── Affichage console ────────────────────────────────────────────────────────

def _fmt_stats(ss: dict) -> str:
    def pct(x):
        return f"{x:.1%}" if isinstance(x, (int, float)) else "–"
    return (f"{ss['avg_goals_per_match']} buts/match (réels {ss.get('real_goals_per_match')}, "
            f"virtuels {ss.get('virtual_goals_per_match')}), nuls {pct(ss['draw_rate'])}, "
            f"dom. {pct(ss['home_win_rate'])}, ext. {pct(ss['away_win_rate'])}, "
            f"Rotaldo {pct(ss.get('rotaldo_rate'))}, note brute tit. {ss.get('starter_rating_mean')}")


def print_odds(res: dict, show_xi: bool = False) -> None:
    ss = res["model"]["sim_stats"]
    print(f"\nCotes {res['label']} — {res['n_sims']} saisons simulées, statut {res['status']} "
          f"(J{res['played_gw']}/{res['total_gw']}, {res['remaining_matches']} matchs restants), "
          f"seed {res['seed']}, coef manager {res['model']['manager_coef']:g}, {res['runtime_s']} s")
    print(f"{'Manager':<10}{'P titre':>8}{'Cote':>7}{'P podium':>10}{'P chapeau':>11}{'Cote':>7}"
          f"{'Pts att.':>9}{'Rang':>6}  {'Compo':<6}{'Force':>6}{'Σbuts':>6}{'ELO':>7}{'Eff.':>5}")
    for m in res["managers"]:
        print(f"{m['name']:<10}{m['p_title']:>7.1%}{m['odd_title'] or 0:>7.1f}{m['p_podium']:>9.1%}"
              f"{m['p_chapeau']:>10.1%}{m['odd_chapeau'] or 0:>7.1f}{m['exp_pts']:>9.1f}{m['exp_rank']:>6.2f}"
              f"  {m['formation']:<6}{m['strength']:>6.2f}{m['xi_goal_rate']:>6.2f}{m['elo']:>7.1f}{m['squad_size']:>5}")
    print(f"sim_stats : {_fmt_stats(ss)}")
    print("  repères réels S15-S18 : 4,1-4,9 buts (réels 2,9-3,6, virtuels 0,9-1,3), nuls 18 %, dom. 45 %, "
          "Rotaldo 2-5 %, note brute tit. 5,26-5,36")
    if ss.get("rating_dist"):
        print("  notes tirées (%) : " + "  ".join(f"{k}→{v}" for k, v in ss["rating_dist"].items()))
    bt = res["model"].get("backtest")
    if bt:
        print(f"backtest {bt['label']} (coef {bt['coef']:g}, {bt['n_sims']} saisons) : champion {bt['champion']} prédit "
              f"#{bt['champion_rank']}, chapeau {bt['chapeau']} prédit #{bt['chapeau_rank']}, Spearman {bt['spearman']:.2f}, "
              f"LL titre {bt['logloss_title']:.2f} / chapeau {bt['logloss_chapeau']:.2f} (naïf {bt['logloss_naive']:.2f})")
    if show_xi:
        for m in res["managers"]:
            print(f"\n  {m['name']} — {m['formation']} ({m['team_name']}) banc {m['bench']}"
                  + (f"  points/match par formation {m['lineup_scores']}" if m.get("lineup_scores") else ""))
            for p in m["xi"]:
                print(f"    {p['pos']} {p['name']:<22} {p['club']:<4} note {p['exp_rating']:.2f}  "
                      f"p_play {p['p_play']:.2f}  buts {p['goal_rate']:.2f}  n={p['n_obs']}/{p['n_ref']}")


def print_backtest(bt: dict) -> None:
    print(f"\nBacktest {bt['label']} — notes {bt['ratings_season']} ≤ J{bt['cutoff_gw']}, "
          f"{bt['n_sims']} saisons, seed {bt['seed']}, coef manager {bt['manager_coef']:g}, {bt['runtime_s']} s")
    if bt["grid"]:
        print(f"{'coef':>5}{'champ#':>8}{'chap#':>7}{'LL titre':>10}{'LL chap.':>10}{'Σ LL':>7}"
              f"{'Spearman':>10}{'MAE pts':>9}{'Brier':>7}{'nuls':>7}{'buts':>6}")
        for g in bt["grid"]:
            print(f"{g['coef']:>5g}{g['champion_rank_pred']:>8}{g['chapeau_rank_pred']:>7}"
                  f"{g['logloss_title']:>10.3f}{g['logloss_chapeau']:>10.3f}"
                  f"{g['logloss_title'] + g['logloss_chapeau']:>7.3f}{g['spearman_exp_pts']:>10.3f}"
                  f"{g['mae_pts']:>9.2f}{g['brier_rank']:>7.3f}{g['sim_stats']['draw_rate']:>7.1%}"
                  f"{g['sim_stats']['avg_goals_per_match']:>6.2f}")
        print(f"coef recommandé (≤ 0,3, tolérance 0,05 pour garder 0) : {bt['recommended_coef']:g}")
    real_by = {r["pid"]: r for r in bt["real"]}
    print(f"\n{'Manager':<10}{'P titre':>8}{'P chapeau':>11}{'Pts att.':>9}{'Rang att.':>10}"
          f"{'Pts réels':>10}{'Rang réel':>10}  Compo  Force  Σbuts")
    for m in bt["pred"]:
        r = real_by.get(m["pid"], {})
        print(f"{m['name']:<10}{m['p_title']:>7.1%}{m['p_chapeau']:>10.1%}{m['exp_pts']:>9.1f}"
              f"{m['exp_rank']:>10.2f}{r.get('points', '?'):>10}{r.get('rank', '?'):>10}  {m['formation']}  "
              f"{m['strength']:.2f}  {m['xi_goal_rate']:.2f}")
    met = bt["metrics"]
    print(f"\nmétriques : champion réel prédit #{met['champion_rank_pred']}, chapeau réel prédit "
          f"#{met['chapeau_rank_pred']}, log-loss titre {met['logloss_title']:.3f} / chapeau "
          f"{met['logloss_chapeau']:.3f} (naïf {met['logloss_naive']:.3f}), Spearman pts {met['spearman_exp_pts']:.3f}, "
          f"MAE pts {met['mae_pts']:.2f}, Brier rang {met['brier_rank']:.3f}")
    print(f"sim_stats : {_fmt_stats(bt['model']['sim_stats'])}")


# ── CLI ──────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="Cotes MPG : simulateur Monte Carlo + backtest")
    ap.add_argument("--division", help="division_id (défaut : courante ; backtest : S18)")
    ap.add_argument("--n", type=int, help="nombre de saisons simulées (défaut 3000 ; backtest 2000)")
    ap.add_argument("--coef", type=float, help=f"coefficient manager (défaut MANAGER_COEF = {MANAGER_COEF})")
    ap.add_argument("--seed", type=int, help="seed (défaut : dérivé des données ; backtest 42)")
    ap.add_argument("--json", help="écrire le résultat dans ce fichier JSON")
    ap.add_argument("--xi", action="store_true", help="afficher le XI attendu de chaque manager")
    ap.add_argument("--blend", choices=("hier", "legacy"), default="hier",
                    help="mélange saison en cours / référence (défaut hier ; legacy = formule initiale)")
    ap.add_argument("--lineup", choices=("sim", "value"), default="sim",
                    help="choix de la formation : sim (points attendus simulés, défaut) ou value (Σ v + bonus défense)")
    ap.add_argument("--max-def", type=int, default=LINEUP_MAX_DEF, choices=(3, 4, 5),
                    help="nombre maximal de défenseurs des formations candidates (défaut %(default)s)")
    ap.add_argument("--l1-season", type=int, default=L1_SEASON, help="saison L1 des notes (défaut %(default)s)")
    ap.add_argument("--ref-season", type=int, help="saison L1 de référence (défaut l1_season − 1)")
    ap.add_argument("--no-backtest", action="store_true", help="ne pas embarquer le backtest S18 dans les cotes")
    ap.add_argument("--backtest", action="store_true", help="backtest aveugle d'une saison terminée")
    ap.add_argument("--cutoff-gw", type=int, default=20, help="backtest : dernière journée L1 connue")
    ap.add_argument("--ratings-season", type=int, default=2025, help="backtest : saison L1 des notes")
    ap.add_argument("--coef-grid", help="backtest : coefficients à comparer, ex. 0,0.1,0.2,0.3,0.5")
    args = ap.parse_args()

    conn = get_conn()
    try:
        if args.backtest:
            grid = [float(x) for x in args.coef_grid.split(",")] if args.coef_grid else None
            bt = backtest(conn, args.division or DEFAULT_BACKTEST_DIVISION, args.ratings_season, args.cutoff_gw,
                          args.n or 2000, 42 if args.seed is None else args.seed, args.coef,
                          coef_grid=grid, blend=args.blend, lineup=args.lineup, max_def=args.max_def)
            print_backtest(bt)
            out = bt
        else:
            res = compute_odds(conn, args.division, args.n or N_SIMS_DEFAULT, args.seed, args.coef,
                               l1_season=args.l1_season, ref_season=args.ref_season, blend=args.blend,
                               lineup=args.lineup, backtest_sims=0 if args.no_backtest else BACKTEST_SIMS_DEFAULT,
                               max_def=args.max_def)
            print_odds(res, show_xi=args.xi)
            out = res
    finally:
        conn.close()
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(out, fh, ensure_ascii=False, indent=1)
        print(f"\n→ {args.json}")


if __name__ == "__main__":
    sys.path.insert(0, ".")
    main()
