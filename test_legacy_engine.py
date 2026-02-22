"""Tests Legacy Analytics Engine.

Prérequis : DB mpg.db populée via --sync-divisions avec toutes les divisions
et people_mapping.yaml appliqué (person_id renseigné sur toutes les équipes).

Usage : python test_legacy_engine.py
"""

import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).parent / "mpg.db"

COVID_DIVISION   = "mpg_division_QU0SUZ6HQPB_6_1"
CURRENT_DIVISION = "mpg_division_QU0SUZ6HQPB_18_1"


def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ── Test 1 : exclusion COVID/incomplet par défaut ─────────────────────────────

def test_covid_exclusion_default():
    """La division COVID+incompl. est exclue par défaut et incluse avec les flags."""
    from mpg_legacy_engine import list_included_divisions

    with _conn() as conn:
        included_default = list_included_divisions(conn, include_covid=False, include_incomplete=False)
        included_all     = list_included_divisions(conn, include_covid=True,  include_incomplete=True, include_current=True)

    assert COVID_DIVISION not in included_default, (
        f"{COVID_DIVISION} ne devrait pas être dans les divisions incluses par défaut"
    )
    with _conn() as conn:
        meta_count = conn.execute("SELECT COUNT(*) AS n FROM divisions_metadata").fetchone()["n"]
    assert len(included_all) == meta_count, (
        f"Avec include_covid+incomplete, attendu {meta_count} divisions, obtenu {len(included_all)}"
    )
    print(
        f"  ✓ exclusion default : {len(included_default)} divisions incluses, "
        f"{len(included_all)} en tout"
    )


# ── Test 2 : exclusion saison en cours par défaut ────────────────────────────

def test_current_exclusion_default():
    """La saison en cours (is_current=1) est exclue par défaut et incluse avec include_current=True."""
    from mpg_legacy_engine import list_included_divisions

    with _conn() as conn:
        included_default  = list_included_divisions(conn)
        included_with_cur = list_included_divisions(conn, include_current=True)

    assert CURRENT_DIVISION not in included_default, (
        f"{CURRENT_DIVISION} ne devrait pas être dans les divisions incluses par défaut"
    )
    assert CURRENT_DIVISION in included_with_cur, (
        f"{CURRENT_DIVISION} devrait apparaître avec include_current=True"
    )
    print(
        f"  ✓ exclusion saison en cours : {len(included_default)} divisions par défaut, "
        f"{len(included_with_cur)} avec include_current=True"
    )


# ── Test 3 : palmarès — 8 personnes ──────────────────────────────────────────

def test_palmares_persons():
    """compute_palmares retourne exactement 8 personnes (8 joueurs mappés)."""
    from mpg_legacy_engine import compute_palmares

    with _conn() as conn:
        rows = compute_palmares(conn)

    assert len(rows) == 8, (
        f"Attendu 8 personnes dans le palmarès, obtenu {len(rows)} : "
        f"{[r['person_id'] for r in rows]}"
    )
    for r in rows:
        assert r["titles"]   >= 0
        assert r["podiums"]  >= r["titles"], "Podiums doit être ≥ titres"
        assert r["chapeaux"] >= 0
        assert r["seasons_played"] > 0, f"{r['person_id']} : seasons_played = 0"

    top3 = [(r["person_id"], r["titles"]) for r in rows[:3]]
    print(f"  ✓ palmarès : 8 personnes — top 3 titres : {top3}")


# ── Test 3 : palmarès — conservation des titres par division MPG ─────────────

def test_palmares_titles_consistency():
    """Il y a exactement 1 titre et 1 chapeau par division MPG complète incluse."""
    from mpg_legacy_engine import compute_palmares, compute_mpg_season_standings

    with _conn() as conn:
        rows     = compute_palmares(conn)
        mpg_data = compute_mpg_season_standings(conn)

    total_titles   = sum(r["titles"]   for r in rows)
    total_chapeaux = sum(r["chapeaux"] for r in rows)
    complete_divs  = sum(1 for d in mpg_data.values() if d["is_complete"])

    assert total_titles > 0, "Aucun titre décerné — données insuffisantes ?"
    assert total_titles == complete_divs, (
        f"Attendu {complete_divs} titres (1 par division complète), obtenu {total_titles}"
    )
    assert total_chapeaux == complete_divs, (
        f"Attendu {complete_divs} chapeaux, obtenu {total_chapeaux}"
    )
    print(
        f"  ✓ titres : {total_titles} = {complete_divs} divisions complètes, "
        f"{total_chapeaux} chapeaux"
    )


# ── Test 4 : palmarès — Pts all-time non identiques ──────────────────────────

def test_palmares_pts_not_identical():
    """Au moins 2 joueurs ont des Pts all-time différents (sinon bug d'agrégation)."""
    from mpg_legacy_engine import compute_palmares

    with _conn() as conn:
        rows = compute_palmares(conn)

    assert len(rows) >= 2, "Pas assez de joueurs pour le test"
    pts_values = [r["all_time_points"] for r in rows]
    assert len(set(pts_values)) >= 2, (
        f"Tous les joueurs ont le même Pts all-time ({pts_values[0]}) — "
        "bug probable dans fetch_matches (outcome non dérivé des scores réels)"
    )
    print(
        f"  ✓ Pts all-time variés : "
        f"min={min(pts_values)}, max={max(pts_values)}, "
        f"valeurs={sorted(set(pts_values))}"
    )


# ── Test 5 : champion et chapeau par division complète ───────────────────────

def test_champion_chapeau_per_division():
    """Pour chaque division complète incluse : champion ≠ chapeau, standings >= 8."""
    from mpg_legacy_engine import compute_mpg_season_standings

    with _conn() as conn:
        mpg_data = compute_mpg_season_standings(conn)

    complete_divs = {d: v for d, v in mpg_data.items() if v["is_complete"]}
    assert len(complete_divs) > 0, "Aucune division complète en DB"

    for div, data in complete_divs.items():
        srows = data["standings"]
        assert len(srows) >= 8, f"{div} : moins de 8 joueurs mappés ({len(srows)})"
        champion = srows[0]["person_id"]
        chapeau  = srows[-1]["person_id"]
        assert champion != chapeau, f"{div} : champion == chapeau ({champion})"
        # Vérifier que le champion a vraiment plus de points que le dernier
        assert srows[0]["points"] >= srows[-1]["points"], \
            f"{div} : classement incohérent"

    print(f"  ✓ champion/chapeau OK sur {len(complete_divs)} divisions complètes")


# ── Test 6 : H2H raph/manu ───────────────────────────────────────────────────

def test_h2h_known_pair():
    """compute_head_to_head ne plante pas et retourne n_matches > 0 pour raph/manu."""
    from mpg_legacy_engine import compute_head_to_head

    with _conn() as conn:
        stats = compute_head_to_head(conn, "raph", "manu")

    assert stats["n_matches"] > 0, (
        "Aucun match H2H trouvé entre raph et manu"
    )
    assert stats["a_wins"] + stats["a_draws"] + stats["a_losses"] == stats["n_matches"], (
        "W+D+L != n_matches pour raph/manu"
    )
    assert stats["home_a"]["n"] + stats["away_a"]["n"] == stats["n_matches"]

    print(
        f"  ✓ H2H raph/manu : {stats['n_matches']} matchs — "
        f"raph {stats['a_wins']}-{stats['a_draws']}-{stats['a_losses']} manu  "
        f"| {stats['a_goals']:.0f}/{stats['b_goals']:.0f}"
    )


# ── Test 7 : H2H symétrique ───────────────────────────────────────────────────

def test_h2h_symmetry():
    """H2H(A,B) et H2H(B,A) sont symétriques."""
    from mpg_legacy_engine import compute_head_to_head

    with _conn() as conn:
        ab = compute_head_to_head(conn, "raph", "manu")
        ba = compute_head_to_head(conn, "manu", "raph")

    assert ab["n_matches"] == ba["n_matches"]
    assert ab["a_wins"]    == ba["a_losses"]
    assert ab["a_losses"]  == ba["a_wins"]
    assert ab["a_draws"]   == ba["a_draws"]
    assert round(ab["goal_diff"] + ba["goal_diff"], 6) == 0.0, (
        f"goal_diff non symétrique : {ab['goal_diff']} + {ba['goal_diff']} ≠ 0"
    )
    print(
        f"  ✓ H2H symétrie : AB={ab['a_wins']}-{ab['a_draws']}-{ab['a_losses']} "
        f"/ BA={ba['a_wins']}-{ba['a_draws']}-{ba['a_losses']}"
    )


# ── Test 8 : ELO — 8 ratings ──────────────────────────────────────────────────

def test_elo_persons():
    """compute_elo retourne exactement 8 ratings."""
    from mpg_legacy_engine import compute_elo

    with _conn() as conn:
        elo = compute_elo(conn)

    assert len(elo) == 8, (
        f"Attendu 8 ratings ELO, obtenu {len(elo)} : {list(elo.keys())}"
    )
    ratings = [v["rating"] for v in elo.values()]
    print(f"  ✓ ELO : {len(elo)} ratings — min={min(ratings):.1f} max={max(ratings):.1f}")


# ── Test 9 : ELO zero-sum ─────────────────────────────────────────────────────

def test_elo_zero_sum():
    """Propriété ELO : moyenne des ratings = 1500 (zero-sum conservé)."""
    from mpg_legacy_engine import compute_elo

    with _conn() as conn:
        elo = compute_elo(conn)

    n     = len(elo)
    total = sum(v["rating"] for v in elo.values())
    avg   = total / n
    assert abs(avg - 1500.0) < 0.1, (
        f"Rating moyen {avg:.3f} ≠ 1500 (propriété zero-sum violée)"
    )
    print(f"  ✓ ELO zero-sum : N={n}, total={total:.1f}, moyenne={avg:.3f}")


# ── Test 10 : ELO — W/L non uniformes ─────────────────────────────────────────

def test_elo_wl_not_identical():
    """Les victoires et défaites varient entre joueurs (sinon bug outcome ELO)."""
    from mpg_legacy_engine import compute_elo

    with _conn() as conn:
        elo = compute_elo(conn)

    assert len(elo) >= 2
    wins_values   = [v["wins"]   for v in elo.values()]
    losses_values = [v["losses"] for v in elo.values()]

    assert len(set(wins_values)) >= 2, (
        f"Tous les joueurs ont le même nombre de victoires ({wins_values[0]}) — "
        "bug probable dans fetch_matches : outcome non dérivé des scores réels"
    )
    assert len(set(losses_values)) >= 2, (
        f"Tous les joueurs ont le même nombre de défaites ({losses_values[0]})"
    )
    print(
        f"  ✓ ELO W variés : [{min(wins_values)}–{max(wins_values)}]  "
        f"L variées : [{min(losses_values)}–{max(losses_values)}]"
    )


# ── Test 11 : resolve_person_id ──────────────────────────────────────────────

def test_resolve_person_id():
    """resolve_person_id accepte person_id, display_name et alias normalisé."""
    from mpg_legacy_engine import resolve_person_id

    cases = [
        ("raph",          "raph"),
        ("Raph",          "raph"),
        ("RAPH",          "raph"),
        ("manu",          "manu"),
        ("Manu",          "manu"),
        ("San Chapo FC",  "raph"),
        ("PIMPAMRAMI",    "manu"),
        ("Inconnu FC",    None),
    ]
    for name, expected in cases:
        result = resolve_person_id(name)
        assert result == expected, (
            f"resolve_person_id({name!r}) → {result!r}, attendu {expected!r}"
        )
    print(f"  ✓ resolve_person_id : {len(cases)} cas validés")


# ── Test : séries V/N/D ───────────────────────────────────────────────────────

def test_streaks():
    """Vérifie la cohérence des séries all-time par joueur."""
    from mpg_legacy_engine import compute_streaks

    with _conn() as conn:
        streaks = compute_streaks(conn)

    assert len(streaks) == 8, f"Attendu 8 joueurs, obtenu {len(streaks)}"

    for pid, s in streaks.items():
        assert s["best_win"] >= 1,      f"{pid} : best_win doit être >= 1"
        assert s["best_loss"] >= 1,     f"{pid} : best_loss doit être >= 1"
        assert s["best_unbeaten"] >= s["best_win"], (
            f"{pid} : best_unbeaten ({s['best_unbeaten']}) < best_win ({s['best_win']})"
        )
        assert s["current_type"] in ("W", "D", "L"), (
            f"{pid} : current_type invalide ({s['current_type']})"
        )
        assert s["current_length"] >= 1, f"{pid} : current_length doit être >= 1"

    wins   = [s["best_win"]      for s in streaks.values()]
    losses = [s["best_loss"]     for s in streaks.values()]
    assert len(set(wins))   > 1, f"best_win identiques pour tous ({wins}) — suspect"
    assert len(set(losses)) > 1, f"best_loss identiques pour tous ({losses}) — suspect"

    best_win_pid = max(streaks, key=lambda p: streaks[p]["best_win"])
    print(
        f"  ✓ séries : 8 joueurs — meilleure série V : "
        f"{best_win_pid} ({streaks[best_win_pid]['best_win']}), "
        f"invaincu max : {max(s['best_unbeaten'] for s in streaks.values())}"
    )


# ── Runner ───────────────────────────────────────────────────────────────────

TESTS = [
    test_covid_exclusion_default,
    test_current_exclusion_default,    # nouveau — détecte le bug is_current ignoré
    test_palmares_persons,
    test_palmares_titles_consistency,
    test_palmares_pts_not_identical,       # nouveau — détecte le bug finalResult
    test_champion_chapeau_per_division,    # nouveau — vérifie champion ≠ chapeau
    test_h2h_known_pair,
    test_h2h_symmetry,
    test_elo_persons,
    test_elo_zero_sum,
    test_elo_wl_not_identical,             # nouveau — détecte W/L uniformes
    test_resolve_person_id,
    test_streaks,                          # nouveau — séries V/N/D all-time
]


def main():
    print(f"Tests Legacy Engine ({DB_PATH})\n{'─' * 50}")
    errors = []
    for test in TESTS:
        name = test.__name__
        try:
            test()
        except Exception as exc:
            errors.append((name, exc))
            print(f"  ✗ {name} : {exc}")

    print(f"\n{'─' * 50}")
    if errors:
        print(f"❌ {len(errors)}/{len(TESTS)} test(s) échoués")
        sys.exit(1)
    else:
        print(f"✅ {len(TESTS)}/{len(TESTS)} tests passés")


if __name__ == "__main__":
    main()
