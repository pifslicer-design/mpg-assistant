"""MPG Legacy Analytics Engine — historique multi-divisions / saisons MPG.

== Saison MPG ==
Une « saison MPG » = une division_id (ex: mpg_division_QU0SUZ6HQPB_17_1).
On raisonne division par division — pas par année civile IRL.
Une saison IRL contient typiquement 2 saisons MPG (deux divisions parallèles).

== Outcome — pourquoi pas finalResult ==
Le champ `finalResult` du JSON MPG vaut toujours 1 (bug API MPG constaté sur
tout l'historique). L'issue réelle est dérivée des scores stockés :
  home_score > away_score  →  1  (victoire domicile)
  home_score < away_score  →  3  (victoire extérieur)
  home_score = away_score  →  2  (nul)

== ELO ==
Algorithme standard K=20, rating initial 1500 :
  expected_A = 1 / (1 + 10^((R_B - R_A) / 400))
  R_A += K * (score_A - expected_A)  avec score ∈ {1.0, 0.5, 0.0}
Ordre déterministe : season ASC, division_id ASC, game_week ASC, match_id ASC.
Propriété zero-sum : Σ ratings = N × 1500 (invariant).
"""

import json
from collections import defaultdict

import yaml

from mpg_db import get_conn
from mpg_people import DEFAULT_MAPPING_PATH, load_people_mapping, normalize_team_name


# ── helpers internes ──────────────────────────────────────────────────────────

def _load_display_names() -> dict[str, str]:
    """Retourne {person_id: display_name} depuis people_mapping.yaml."""
    data = yaml.safe_load(DEFAULT_MAPPING_PATH.read_text(encoding="utf-8"))
    return {
        pid: info.get("display_name", pid)
        for pid, info in data.get("persons", {}).items()
    }


def resolve_person_id(name: str) -> str | None:
    """Résout un identifiant CLI en person_id.

    Accepte, dans l'ordre :
    1. person_id direct, insensible à la casse (ex: « raph »)
    2. display_name, insensible à la casse (ex: « Raph »)
    3. alias normalisé via people_mapping.yaml (ex: « San Chapo FC »)
    """
    name_stripped = name.strip()
    data = yaml.safe_load(DEFAULT_MAPPING_PATH.read_text(encoding="utf-8"))
    persons = data.get("persons", {})

    for pid in persons:
        if pid.casefold() == name_stripped.casefold():
            return pid

    for pid, info in persons.items():
        dn = info.get("display_name", pid)
        if dn.casefold() == name_stripped.casefold():
            return pid

    mapping = load_people_mapping(DEFAULT_MAPPING_PATH)
    result = mapping.get(normalize_team_name(name_stripped))
    if result:
        return result[0]

    return None


def _empty_record() -> dict:
    return {
        "wins": 0, "draws": 0, "losses": 0, "points": 0,
        "goals_for": 0.0, "goals_against": 0.0, "matches_played": 0,
    }


def _apply_result(
    record: dict, is_home: bool, gf: float, ga: float, outcome: int
) -> None:
    """Met à jour W/D/L/Pts/BP/BC (outcome : 1=home win, 2=draw, 3=away win)."""
    record["matches_played"] += 1
    record["goals_for"]      += gf
    record["goals_against"]  += ga
    if outcome == 2:
        record["draws"]  += 1
        record["points"] += 1
    elif (outcome == 1 and is_home) or (outcome == 3 and not is_home):
        record["wins"]   += 1
        record["points"] += 3
    else:
        record["losses"] += 1


# ── API publique ──────────────────────────────────────────────────────────────

def list_included_divisions(
    conn,
    include_covid: bool = False,
    include_incomplete: bool = False,
    include_current: bool = False,
) -> list[str]:
    """Retourne les division_ids validées selon les flags d'inclusion."""
    clauses = ["1=1"]
    if not include_covid:
        clauses.append("is_covid=0")
    if not include_incomplete:
        clauses.append("is_incomplete=0")
    if not include_current:
        clauses.append("is_current=0")
    where = " AND ".join(clauses)
    rows = conn.execute(
        f"SELECT division_id FROM divisions_metadata WHERE {where} ORDER BY season, division_id"
    ).fetchall()
    return [r["division_id"] for r in rows]


def fetch_matches(conn, division_ids: list[str]) -> list[dict]:
    """Charge les matchs joués des divisions demandées.

    L'outcome est dérivé des scores réels (le champ finalResult du JSON MPG
    vaut toujours 1, il est donc ignoré) :
      home_score > away_score → 1 (home win)
      home_score < away_score → 3 (away win)
      home_score = away_score → 2 (draw)

    Seuls les matchs avec home_score ET away_score non nuls sont inclus.
    Retourne une liste triée (season, division_id, game_week, match_id) pour
    garantir l'ordre déterministe requis par l'ELO.
    """
    if not division_ids:
        return []

    ph = ",".join("?" * len(division_ids))
    rows = conn.execute(f"""
        SELECT
            m.id            AS match_id,
            m.season,
            m.division_id,
            m.game_week,
            m.home_score,
            m.away_score,
            ht.person_id    AS home_person_id,
            at.person_id    AS away_person_id
        FROM matches m
        LEFT JOIN teams ht ON m.home_team_id = ht.id
        LEFT JOIN teams at ON m.away_team_id = at.id
        WHERE m.division_id IN ({ph})
          AND m.home_score IS NOT NULL
          AND m.away_score IS NOT NULL
        ORDER BY m.season ASC, m.division_id ASC, m.game_week ASC, m.id ASC
    """, division_ids).fetchall()

    result = []
    for r in rows:
        hs  = float(r["home_score"])
        as_ = float(r["away_score"])
        if hs > as_:
            outcome = 1
        elif as_ > hs:
            outcome = 3
        else:
            outcome = 2
        result.append({
            "match_id":       r["match_id"],
            "season":         r["season"],
            "division_id":    r["division_id"],
            "game_week":      r["game_week"],
            "home_score":     hs,
            "away_score":     as_,
            "home_person_id": r["home_person_id"],
            "away_person_id": r["away_person_id"],
            "final_result":   outcome,
        })
    return result


def compute_mpg_season_standings(
    conn,
    include_covid: bool = False,
    include_incomplete: bool = False,
) -> dict[str, dict]:
    """Classement par division_id (= une saison MPG).

    Retourne {division_id: {
        "season":      int,   # année IRL de démarrage
        "n_matches":   int,   # matchs joués en DB
        "is_complete": bool,  # n_matches >= 56 ET >= 8 joueurs mappés
        "standings":   list,  # triée par rang (Pts DESC, Diff DESC, BP DESC)
    }}

    Chaque row de standings :
      {person_id, matches_played, wins, draws, losses, points,
       goals_for, goals_against, goal_diff, avg_pts}

    avg_pts = points / matches_played  (MoyPts, pas moyenne de buts).
    """
    divisions = list_included_divisions(conn, include_covid, include_incomplete)
    if not divisions:
        return {}

    ph = ",".join("?" * len(divisions))
    meta = conn.execute(
        f"SELECT division_id, season, n_matches FROM divisions_metadata WHERE division_id IN ({ph})",
        divisions,
    ).fetchall()
    div_info: dict[str, dict] = {
        r["division_id"]: {"season": r["season"], "n_matches": r["n_matches"]}
        for r in meta
    }

    matches = fetch_matches(conn, divisions)

    # div_standings[division_id][person_id] = record
    div_standings: dict = defaultdict(lambda: defaultdict(_empty_record))
    for m in matches:
        div = m["division_id"]
        hp, ap = m["home_person_id"], m["away_person_id"]
        if not hp or not ap:
            continue
        _apply_result(div_standings[div][hp], True,  m["home_score"], m["away_score"], m["final_result"])
        _apply_result(div_standings[div][ap], False, m["away_score"], m["home_score"], m["final_result"])

    result: dict[str, dict] = {}
    # Ordre chronologique : season ASC, division_id ASC
    for div in sorted(divisions, key=lambda d: (div_info.get(d, {}).get("season", 0), d)):
        if div not in div_standings:
            continue
        info      = div_info.get(div, {})
        n_matches = info.get("n_matches", 0)
        rows = []
        for person_id, s in div_standings[div].items():
            row = dict(s)
            row["person_id"] = person_id
            row["goal_diff"] = row["goals_for"] - row["goals_against"]
            mp = row["matches_played"]
            row["avg_pts"]   = round(row["points"] / mp, 2) if mp else 0.0
            rows.append(row)
        rows.sort(key=lambda r: (-r["points"], -r["goal_diff"], -r["goals_for"]))
        result[div] = {
            "season":      info.get("season"),
            "n_matches":   n_matches,
            "is_complete": n_matches >= 56 and len(rows) >= 8,
            "standings":   rows,
        }

    return result


def compute_palmares(
    conn,
    include_covid: bool = False,
    include_incomplete: bool = False,
) -> list[dict]:
    """Palmarès all-time par person_id, basé sur les saisons MPG (divisions).

    Titres / podiums / chapeaux : uniquement pour les divisions « complètes »
    (n_matches >= 56 ET >= 8 joueurs mappés dans la division).
    Points et matches all-time : toutes les divisions incluses.

    Moy = all_time_points / all_time_matches  (MoyPts, pas moyenne de buts).

    Retourne une liste triée (titres DESC, podiums DESC, pts DESC) :
      [{person_id, titles, podiums, chapeaux, seasons_played,
        all_time_points, all_time_matches, all_time_goals_for, all_time_avg_pts}]
    """
    mpg_standings = compute_mpg_season_standings(conn, include_covid, include_incomplete)

    palmares: dict[str, dict] = defaultdict(lambda: {
        "titles": 0, "podiums": 0, "chapeaux": 0, "seasons_played": 0,
        "all_time_points": 0, "all_time_matches": 0, "all_time_goals_for": 0.0,
    })

    for div, data in mpg_standings.items():
        rows        = data["standings"]
        is_complete = data["is_complete"]
        n           = len(rows)

        for i, row in enumerate(rows):
            pid = row["person_id"]
            p   = palmares[pid]
            p["seasons_played"]     += 1
            p["all_time_points"]    += row["points"]
            p["all_time_matches"]   += row["matches_played"]
            p["all_time_goals_for"] += row["goals_for"]

            if is_complete:
                if i == 0:
                    p["titles"]  += 1
                    p["podiums"] += 1
                elif i < 3:
                    p["podiums"] += 1
                if i == n - 1:
                    p["chapeaux"] += 1

    result = []
    for pid, p in palmares.items():
        row = dict(p)
        row["person_id"]        = pid
        m = row["all_time_matches"]
        row["all_time_avg_pts"] = round(row["all_time_points"] / m, 2) if m else 0.0
        result.append(row)
    result.sort(key=lambda r: (-r["titles"], -r["podiums"], -r["all_time_points"]))
    return result


def compute_head_to_head(
    conn,
    person_a: str,
    person_b: str,
    include_covid: bool = False,
    include_incomplete: bool = False,
) -> dict:
    """Stats H2H entre deux person_ids.

    Retourne :
      {n_matches, a_wins, a_draws, a_losses,
       a_goals, b_goals, goal_diff,
       home_a: {n, wins, draws, losses, goals_for, goals_against},
       away_a: {n, wins, draws, losses, goals_for, goals_against}}
    """
    divisions = list_included_divisions(conn, include_covid, include_incomplete)
    matches   = fetch_matches(conn, divisions)

    def _side() -> dict:
        return {"n": 0, "wins": 0, "draws": 0, "losses": 0,
                "goals_for": 0.0, "goals_against": 0.0}

    stats: dict = {
        "n_matches": 0, "a_wins": 0, "a_draws": 0, "a_losses": 0,
        "a_goals": 0.0, "b_goals": 0.0, "goal_diff": 0.0,
        "home_a": _side(), "away_a": _side(),
    }

    for m in matches:
        hp, ap = m["home_person_id"], m["away_person_id"]
        is_ab = (hp == person_a and ap == person_b)
        is_ba = (hp == person_b and ap == person_a)
        if not is_ab and not is_ba:
            continue

        stats["n_matches"] += 1
        fr = m["final_result"]

        if is_ab:
            gf_a, gf_b = m["home_score"], m["away_score"]
            a_wins  = (fr == 1)
            a_draws = (fr == 2)
            side    = stats["home_a"]
        else:
            gf_a, gf_b = m["away_score"], m["home_score"]
            a_wins  = (fr == 3)
            a_draws = (fr == 2)
            side    = stats["away_a"]

        stats["a_goals"] += gf_a
        stats["b_goals"] += gf_b
        if a_wins:
            stats["a_wins"]  += 1; side["wins"]   += 1
        elif a_draws:
            stats["a_draws"] += 1; side["draws"]  += 1
        else:
            stats["a_losses"] += 1; side["losses"] += 1

        side["n"]             += 1
        side["goals_for"]     += gf_a
        side["goals_against"] += gf_b

    stats["goal_diff"] = round(stats["a_goals"] - stats["b_goals"], 2)
    return stats


def compute_elo(
    conn,
    k: int = 20,
    include_covid: bool = False,
    include_incomplete: bool = False,
) -> dict[str, dict]:
    """Ratings ELO all-time par person_id.

    ELO standard K=20, rating initial 1500.
    Traitement chronologique : season ASC, division_id ASC, game_week ASC, match_id ASC.
    Propriété zero-sum : Σ ratings = N × 1500 (invariant garanti par la symétrie des updates).

    Retourne {person_id: {rating, matches_played, wins, draws, losses}}.
    """
    divisions = list_included_divisions(conn, include_covid, include_incomplete)
    matches   = fetch_matches(conn, divisions)

    ratings: dict[str, float] = defaultdict(lambda: 1500.0)
    records: dict[str, dict]  = defaultdict(
        lambda: {"wins": 0, "draws": 0, "losses": 0, "matches_played": 0}
    )

    for m in matches:
        hp, ap = m["home_person_id"], m["away_person_id"]
        if not hp or not ap:
            continue

        r_h, r_a = ratings[hp], ratings[ap]
        exp_h = 1.0 / (1.0 + 10.0 ** ((r_a - r_h) / 400.0))

        fr = m["final_result"]
        if fr == 1:
            score_h, score_a = 1.0, 0.0
        elif fr == 2:
            score_h, score_a = 0.5, 0.5
        else:
            score_h, score_a = 0.0, 1.0

        ratings[hp] = r_h + k * (score_h - exp_h)
        ratings[ap] = r_a + k * (score_a - (1.0 - exp_h))

        records[hp]["matches_played"] += 1
        records[ap]["matches_played"] += 1
        if fr == 1:
            records[hp]["wins"]   += 1; records[ap]["losses"] += 1
        elif fr == 2:
            records[hp]["draws"]  += 1; records[ap]["draws"]  += 1
        else:
            records[hp]["losses"] += 1; records[ap]["wins"]   += 1

    return {
        pid: {
            "rating":         round(ratings[pid], 1),
            "matches_played": records[pid]["matches_played"],
            "wins":           records[pid]["wins"],
            "draws":          records[pid]["draws"],
            "losses":         records[pid]["losses"],
        }
        for pid in set(records)
    }


def compute_streaks(
    conn,
    include_covid: bool = False,
    include_incomplete: bool = False,
) -> dict[str, dict]:
    """Séries consécutives all-time par person_id (cross-divisions).

    Retourne {person_id: {
        best_win:        int,   # plus longue série de victoires
        best_unbeaten:   int,   # plus longue série sans défaite (V+N)
        best_loss:       int,   # plus longue série de défaites
        current_type:    str,   # 'W' | 'D' | 'L' — dernier résultat de la timeline
        current_length:  int,   # longueur de la série en cours
    }}

    Les séries enjambent les divisions (même groupe de 8 joueurs, continuité
    chronologique garantie par fetch_matches ORDER BY season, division_id, gw, id).
    """
    divisions = list_included_divisions(conn, include_covid, include_incomplete)
    matches   = fetch_matches(conn, divisions)

    seq_by_player: dict[str, list[str]] = defaultdict(list)
    for m in matches:
        hp, ap = m["home_person_id"], m["away_person_id"]
        if not hp or not ap:
            continue
        fr = m["final_result"]
        if fr == 1:
            seq_by_player[hp].append("W")
            seq_by_player[ap].append("L")
        elif fr == 2:
            seq_by_player[hp].append("D")
            seq_by_player[ap].append("D")
        else:
            seq_by_player[hp].append("L")
            seq_by_player[ap].append("W")

    result: dict[str, dict] = {}
    for pid, seq in seq_by_player.items():
        best_win = best_unbeaten = best_loss = 0
        cur_win = cur_unbeaten = cur_loss = 0

        for r in seq:
            if r == "W":
                cur_win += 1
                cur_unbeaten += 1
                cur_loss = 0
            elif r == "D":
                cur_win = 0
                cur_unbeaten += 1
                cur_loss = 0
            else:  # L
                cur_win = 0
                cur_unbeaten = 0
                cur_loss += 1

            best_win      = max(best_win, cur_win)
            best_unbeaten = max(best_unbeaten, cur_unbeaten)
            best_loss     = max(best_loss, cur_loss)

        # Série en cours : remonter la fin de la timeline
        cur_type   = seq[-1] if seq else None
        cur_length = 0
        for r in reversed(seq):
            if r == cur_type:
                cur_length += 1
            else:
                break

        result[pid] = {
            "best_win":      best_win,
            "best_unbeaten": best_unbeaten,
            "best_loss":     best_loss,
            "current_type":  cur_type,
            "current_length": cur_length,
        }

    return result


# ── rapports console ──────────────────────────────────────────────────────────

def print_streaks_report(
    include_covid: bool = False,
    include_incomplete: bool = False,
) -> None:
    """Affiche les meilleures séries V/N/D par joueur + série en cours."""
    display = _load_display_names()
    with get_conn() as conn:
        streaks = compute_streaks(conn, include_covid, include_incomplete)

    if not streaks:
        print("[SÉRIES] Aucune donnée.")
        return

    excl = []
    if not include_covid:
        excl.append("COVID exclus")
    if not include_incomplete:
        excl.append("incomplets exclus")
    excl_label = ", ".join(excl) or "tout inclus"

    sorted_rows = sorted(
        streaks.items(),
        key=lambda x: (-x[1]["best_win"], -x[1]["best_unbeaten"]),
    )

    LABELS = {"W": "V", "D": "N", "L": "D"}
    print(f"\n=== Séries consécutives ({excl_label}) ===")
    col = 12
    header = (
        f"  {'Joueur':<{col}} {'Série V':>7} {'Invaincu':>8} {'Série D':>7} {'En cours':>9}"
    )
    print(header)
    print("  " + "-" * (len(header) - 2))
    for pid, s in sorted_rows:
        name    = display.get(pid, pid)[:col - 1]
        cur_lbl = f"{s['current_length']}{LABELS.get(s['current_type'], '?')}"
        print(
            f"  {name:<{col}} {s['best_win']:>7} {s['best_unbeaten']:>8} "
            f"{s['best_loss']:>7} {cur_lbl:>9}"
        )
    print()


def print_palmares_report(
    include_covid: bool = False,
    include_incomplete: bool = False,
) -> None:
    """Affiche le palmarès all-time + champions/chapeaux par division MPG."""
    display = _load_display_names()
    with get_conn() as conn:
        rows     = compute_palmares(conn, include_covid, include_incomplete)
        mpg_data = compute_mpg_season_standings(conn, include_covid, include_incomplete)

    excl = []
    if not include_covid:
        excl.append("COVID exclus")
    if not include_incomplete:
        excl.append("incomplets exclus")
    excl_label = ", ".join(excl) or "tout inclus"

    print(f"\n=== Palmarès all-time ({excl_label}) ===")
    if not rows:
        print("  [aucune donnée]")
        return

    col = 12
    header = (
        f"  {'Joueur':<{col}} {'Titres':>6} {'Podiums':>7} {'Chapeaux':>8} "
        f"{'Saisons':>7} {'Pts':>6} {'Moy':>6}"
    )
    print(header)
    print("  " + "-" * (len(header) - 2))
    for r in rows:
        name = display.get(r["person_id"], r["person_id"])[:col - 1]
        print(
            f"  {name:<{col}} {r['titles']:>6} {r['podiums']:>7} {r['chapeaux']:>8} "
            f"{r['seasons_played']:>7} {r['all_time_points']:>6} {r['all_time_avg_pts']:>6.2f}"
        )

    # Résumé par division MPG (complètes seulement)
    complete_divs = [(d, v) for d, v in mpg_data.items() if v["is_complete"]]
    if complete_divs:
        print(f"\n  Champions/Chapeaux ({len(complete_divs)} divisions complètes) :")
        for div, data in complete_divs:
            srows = data["standings"]
            if len(srows) >= 2:
                champ = display.get(srows[0]["person_id"], srows[0]["person_id"])
                last  = display.get(srows[-1]["person_id"], srows[-1]["person_id"])
                # Extrait le suffixe court : mpg_division_XXXXX_18_1 → "S18.1"
                parts = div.rsplit("_", 2)
                tag   = f"S{parts[-2]}.{parts[-1]}"
                print(f"    [{data['season']}] {tag} : 1er {champ}  |  dernier {last}")
    print()


def print_elo_report(
    include_covid: bool = False,
    include_incomplete: bool = False,
) -> None:
    """Affiche le classement ELO."""
    display = _load_display_names()
    with get_conn() as conn:
        elo = compute_elo(conn, include_covid=include_covid, include_incomplete=include_incomplete)

    if not elo:
        print("[ELO] Aucune donnée.")
        return

    excl = []
    if not include_covid:
        excl.append("COVID exclus")
    if not include_incomplete:
        excl.append("incomplets exclus")
    excl_label = ", ".join(excl) or "tout inclus"

    sorted_elo = sorted(elo.items(), key=lambda x: -x[1]["rating"])
    print(f"\n=== ELO ({excl_label}) ===")
    col = 12
    header = f"  {'Rang':>4} {'Joueur':<{col}} {'Rating':>7} {'J':>4} {'V':>4} {'N':>4} {'D':>4}"
    print(header)
    print("  " + "-" * (len(header) - 2))
    for rank, (pid, s) in enumerate(sorted_elo, 1):
        name = display.get(pid, pid)[:col - 1]
        print(
            f"  {rank:>4} {name:<{col}} {s['rating']:>7.1f} "
            f"{s['matches_played']:>4} {s['wins']:>4} {s['draws']:>4} {s['losses']:>4}"
        )
    print()


def print_h2h_report(
    person_a: str,
    person_b: str,
    include_covid: bool = False,
    include_incomplete: bool = False,
) -> None:
    """Affiche les stats H2H entre deux joueurs."""
    display = _load_display_names()
    name_a  = display.get(person_a, person_a)
    name_b  = display.get(person_b, person_b)
    with get_conn() as conn:
        stats = compute_head_to_head(
            conn, person_a, person_b,
            include_covid=include_covid, include_incomplete=include_incomplete,
        )

    n = stats["n_matches"]
    print(f"\n=== H2H : {name_a} vs {name_b} ===")
    if n == 0:
        print("  Aucun match trouvé.")
        print()
        return

    diff_str = f"{stats['goal_diff']:+.0f}"
    print(
        f"  {n} matchs  |  "
        f"{name_a} {stats['a_wins']}-{stats['a_draws']}-{stats['a_losses']} {name_b}"
        f"  |  {stats['a_goals']:.0f}/{stats['b_goals']:.0f} buts  DIFF {diff_str}"
    )
    ha, aa = stats["home_a"], stats["away_a"]
    if ha["n"]:
        print(
            f"  · {name_a} dom. : {ha['n']} matchs — "
            f"V{ha['wins']} N{ha['draws']} D{ha['losses']} — "
            f"{ha['goals_for']:.0f}/{ha['goals_against']:.0f}"
        )
    if aa["n"]:
        print(
            f"  · {name_a} ext. : {aa['n']} matchs — "
            f"V{aa['wins']} N{aa['draws']} D{aa['losses']} — "
            f"{aa['goals_for']:.0f}/{aa['goals_against']:.0f}"
        )
    print()


def print_mpg_season_report(division_id: str) -> None:
    """Affiche le classement J/V/N/D/Pts/BP/BC/Diff d'une division MPG."""
    display = _load_display_names()
    with get_conn() as conn:
        meta = conn.execute(
            "SELECT season, n_matches, is_incomplete FROM divisions_metadata WHERE division_id=?",
            (division_id,)
        ).fetchone()
        matches = fetch_matches(conn, [division_id])

    if not matches:
        print(f"[SEASON-MPG] Aucun match joué trouvé pour {division_id}.")
        return

    standings: dict = defaultdict(_empty_record)
    for m in matches:
        hp, ap = m["home_person_id"], m["away_person_id"]
        if not hp or not ap:
            continue
        _apply_result(standings[hp], True,  m["home_score"], m["away_score"], m["final_result"])
        _apply_result(standings[ap], False, m["away_score"], m["home_score"], m["final_result"])

    rows = []
    for person_id, s in standings.items():
        row = dict(s)
        row["person_id"] = person_id
        row["goal_diff"] = row["goals_for"] - row["goals_against"]
        mp = row["matches_played"]
        row["avg_pts"]   = round(row["points"] / mp, 2) if mp else 0.0
        rows.append(row)
    rows.sort(key=lambda r: (-r["points"], -r["goal_diff"], -r["goals_for"]))

    season    = meta["season"]    if meta else "?"
    n_matches = meta["n_matches"] if meta else len(matches)
    status    = " [incomplet]" if meta and meta["is_incomplete"] else ""

    print(f"\n=== Saison MPG : {division_id} (IRL {season}, {n_matches} matchs{status}) ===")
    col = 12
    header = (
        f"  {'Rang':>4} {'Joueur':<{col}} "
        f"{'J':>3} {'V':>3} {'N':>3} {'D':>3} {'Pts':>4} "
        f"{'BP':>6} {'BC':>6} {'Diff':>6} {'Moy':>5}"
    )
    print(header)
    print("  " + "-" * (len(header) - 2))
    for rank, row in enumerate(rows, 1):
        name = display.get(row["person_id"], row["person_id"])[:col - 1]
        diff = f"{row['goal_diff']:+.1f}"
        print(
            f"  {rank:>4} {name:<{col}} "
            f"{row['matches_played']:>3} {row['wins']:>3} {row['draws']:>3} {row['losses']:>3} "
            f"{row['points']:>4} "
            f"{row['goals_for']:>6.1f} {row['goals_against']:>6.1f} "
            f"{diff:>6} {row['avg_pts']:>5.2f}"
        )
    print()
