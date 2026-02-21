"""Tests batch import : season, COVID exclusion, people mapping.

Usage : python test_batch_import.py
"""

import json
import sys
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "mpg.db"


def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ── Test 1 : colonne season remplie ─────────────────────────────────────────

def test_season_column():
    with _conn() as conn:
        total = conn.execute("SELECT COUNT(*) AS n FROM matches").fetchone()["n"]
        with_season = conn.execute(
            "SELECT COUNT(*) AS n FROM matches WHERE season IS NOT NULL"
        ).fetchone()["n"]
        seasons = [
            r["season"]
            for r in conn.execute(
                "SELECT DISTINCT season FROM matches WHERE season IS NOT NULL ORDER BY season"
            ).fetchall()
        ]

    assert total > 0, "Aucun match en DB"
    assert with_season == total, (
        f"season NULL sur {total - with_season}/{total} matchs"
    )
    print(f"  ✓ season remplie sur {total} matchs — valeurs : {seasons}")


# ── Test 2 : divisions-file simulé ──────────────────────────────────────────

def test_divisions_file_parsing():
    content = """
# commentaire ignoré
mpg_division_QU0SUZ6HQPB_18_1

mpg_division_QU0SUZ6HQPB_17_1
"""
    divisions = [
        l.strip() for l in content.strip().splitlines()
        if l.strip() and not l.startswith("#")
    ]
    assert len(divisions) == 2, f"Attendu 2 divisions, obtenu {len(divisions)}"
    print(f"  ✓ parsing divisions-file : {divisions}")


# ── Test 3 : exclusion COVID dans les stats ──────────────────────────────────

def test_covid_exclusion():
    from mpg_stats import compute_records, COVID_SEASON

    # Sans COVID
    records_no_covid = compute_records(include_covid=False)
    # Avec COVID
    records_with_covid = compute_records(include_covid=True)

    # Vérifier qu'aucun match saison 6 n'entre dans le calcul par défaut
    with _conn() as conn:
        covid_matches = conn.execute(
            "SELECT COUNT(*) AS n FROM matches WHERE season=?", (COVID_SEASON,)
        ).fetchone()["n"]

    if covid_matches > 0:
        total_no  = sum(s["matches_played"] for s in records_no_covid.values())
        total_yes = sum(s["matches_played"] for s in records_with_covid.values())
        # Chaque match compte 2 fois (home + away)
        expected_diff = covid_matches * 2
        assert total_yes - total_no == expected_diff, (
            f"Différence matchs COVID attendue={expected_diff}, "
            f"obtenue={total_yes - total_no}"
        )
        print(f"  ✓ exclusion COVID : {covid_matches} matchs saison {COVID_SEASON} correctement filtrés")
    else:
        print(f"  ✓ exclusion COVID : aucun match saison {COVID_SEASON} en DB (pas encore importé)")


# ── Test 4 : people mapping ──────────────────────────────────────────────────

def test_normalize_team_name():
    from mpg_people import normalize_team_name

    cases = [
        # (input, expected_normalized)
        ("NAPPY FC ",      "nappy fc"),    # trailing space
        ("  NAPPY  FC",    "nappy fc"),    # espaces multiples + leading
        ("nappy fc",       "nappy fc"),    # déjà normalisé
        ("Étoile  FC",     "etoile fc"),   # accents + espaces multiples
        ("PIMPAMRAMI",     "pimpamrami"),
        ("Miller FC",      "miller fc"),
        ("FC Miller",      "fc miller"),   # ordre différent : non équivalent (attendu)
        # Ponctuation simple
        ("Chien Chaud FC", "chien chaud fc"),  # aucun changement (pas de ponctuation)
        ("Chien chaud FC", "chien chaud fc"),  # casse différente → même résultat
        ("O'Brien FC",     "o brien fc"),      # apostrophe droite → espace
        ("O\u2019Brien",   "o brien"),         # apostrophe typographique → espace
        ("FC-Milano",      "fc milano"),       # tiret → espace
    ]
    for s, expected in cases:
        result = normalize_team_name(s)
        assert result == expected, f"normalize({s!r}) → {result!r}, attendu {expected!r}"
    print(f"  ✓ normalize_team_name : {len(cases)} cas validés")


def test_people_mapping():
    from mpg_people import load_people_mapping, resolve_person

    mapping = load_people_mapping()

    cases = [
        # Noms exacts
        ("San Chapo FC",         "raph"),
        ("PIMPAMRAMI",           "manu"),
        ("Lulu FC",              "pierre"),
        ("Cup",                  "greg"),
        # Normalisation espaces / casse
        ("NAPPY FC ",            "greg"),   # trailing space → doit matcher
        ("nappy fc",             "greg"),   # tout lowercase → doit matcher
        ("Miller FC",            "marc"),   # alias ajouté
        ("miller fc",            "marc"),   # casse différente
        ("issy ci boubou",       "raph"),
        ("Lulu Football Club",   "pierre"),
        ("Stade Malherbe de Milan", "damien"),
        ("Les Malabars",         "francois"),
        ("Puntagliera",          "nico"),
        # Nouveaux aliases (cases réelles non mappées avant ce patch)
        ("Chien Chaud FC",       "francois"),  # alias avec suffixe FC
        ("Chien chaud FC",       "francois"),  # casse différente → même normalisé
        ("Punta",                "nico"),       # alias court
        # Inconnu
        ("Inconnu FC",           None),
    ]
    for team_name, expected_person in cases:
        result = resolve_person(team_name, mapping)
        person_id = result[0] if result else None
        assert person_id == expected_person, (
            f"resolve_person({team_name!r}) → {person_id!r}, attendu {expected_person!r}"
        )
    print(f"  ✓ people mapping : {len(cases)} cas validés")


def test_enrich_division_pwn():
    """Vérifie que la division PWN77AILXZQ_2_1 est mappée à 8/8 si elle est en DB."""
    from mpg_people import load_people_mapping, enrich_teams_with_person_id

    DIV = "mpg_division_PWN77AILXZQ_2_1"
    with _conn() as conn:
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM teams WHERE division_id=?", (DIV,)
        ).fetchone()["n"]

    if n == 0:
        print(f"  ⚠ division {DIV} absente de la DB — test skippé")
        return

    mapping = load_people_mapping()
    result = enrich_teams_with_person_id(division_id=DIV, mapping=mapping)
    assert result["unmapped"] == 0, (
        f"Division PWN : {result['unmapped']} équipe(s) non mappée(s)"
    )
    assert result["mapped"] == 8, (
        f"Division PWN : {result['mapped']} équipes mappées (attendu 8)"
    )
    print(f"  ✓ enrich PWN77AILXZQ_2_1 : 8/8 équipes mappées")


def test_zero_unmapped_in_included_divisions():
    """Aucune équipe des divisions incluses (hors COVID/incomplet) ne doit avoir person_id IS NULL.

    Ce test garantit que people_mapping.yaml couvre tous les noms historiques.
    Il nécessite que _apply_people_mapping() ait déjà été exécuté sur la DB.
    """
    from mpg_people import load_people_mapping, enrich_teams_with_person_id

    # Réapplique le mapping sur toutes les équipes (idempotent)
    mapping = load_people_mapping()
    enrich_teams_with_person_id(division_id=None, mapping=mapping)

    with _conn() as conn:
        # Divisions incluses par défaut (hors COVID, hors incomplètes)
        included_rows = conn.execute("""
            SELECT division_id FROM divisions_metadata
            WHERE is_covid=0 AND is_incomplete=0
        """).fetchall()
        included = [r["division_id"] for r in included_rows]

        if not included:
            print("  ⚠ Aucune division incluse en DB — test skippé")
            return

        ph = ",".join("?" * len(included))
        unmapped = conn.execute(f"""
            SELECT division_id, name FROM teams
            WHERE person_id IS NULL AND division_id IN ({ph})
            ORDER BY division_id, name
        """, included).fetchall()

    if unmapped:
        by_div: dict[str, list[str]] = {}
        for r in unmapped:
            by_div.setdefault(r["division_id"], []).append(r["name"])
        details = "; ".join(f"{d}→{ns}" for d, ns in sorted(by_div.items()))
        raise AssertionError(
            f"{len(unmapped)} équipe(s) non mappée(s) dans les divisions incluses : {details}"
        )

    total_teams = conn.execute(
        f"SELECT COUNT(*) AS n FROM teams WHERE division_id IN ({ph})", included
    ).fetchone()["n"]
    print(f"  ✓ zéro équipe non mappée ({total_teams} équipes, {len(included)} divisions incluses)")


# ── Runner ───────────────────────────────────────────────────────────────────

TESTS = [
    test_season_column,
    test_divisions_file_parsing,
    test_covid_exclusion,
    test_normalize_team_name,
    test_people_mapping,
    test_enrich_division_pwn,
    test_zero_unmapped_in_included_divisions,
]


def main():
    print(f"Tests batch import ({DB_PATH})\n{'─' * 50}")
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
