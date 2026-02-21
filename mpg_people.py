"""MPG — normalisation identité IRL via people_mapping.yaml."""

import re
import unicodedata
from pathlib import Path

import yaml
from mpg_db import get_conn

DEFAULT_MAPPING_PATH = Path(__file__).parent / "people_mapping.yaml"
COVID_SEASON = 6


def normalize_team_name(s: str) -> str:
    """Normalise un nom d'équipe pour la comparaison fuzzy.

    Applique dans l'ordre :
    1. Strip espaces début/fin + collapse espaces multiples → 1 espace
    2. Casefold (plus agressif que lower() pour l'Unicode)
    3. Suppression des accents (NFD decomposition)
    4. Ponctuation simple (apostrophes, tirets, underscores, points) → espace
    5. Re-collapse espaces après substitution
    """
    s = re.sub(r'\s+', ' ', s.strip())
    s = s.casefold()
    s = unicodedata.normalize('NFD', s)
    s = ''.join(c for c in s if unicodedata.category(c) != 'Mn')
    # Apostrophes (droites et typographiques) et séparateurs → espace
    s = re.sub(r"['\u2019\u2018`\-_\.]", ' ', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def load_people_mapping(path: str | Path = DEFAULT_MAPPING_PATH) -> dict:
    """Charge le fichier YAML.

    Retourne {alias_normalized: (person_id, display_name)}.
    L'index est pré-calculé avec normalize_team_name pour éviter toute
    recomputation à chaque appel de resolve_person.
    """
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    index: dict[str, tuple[str, str]] = {}
    for person_id, info in data.get("persons", {}).items():
        display = info.get("display_name", person_id)
        for alias in info.get("aliases", []):
            index[normalize_team_name(alias)] = (person_id, display)
    return index


def resolve_person(team_name: str, mapping: dict) -> tuple[str, str] | None:
    """Retourne (person_id, display_name) ou None.

    Normalise le nom avant lookup — tolérant aux espaces et à la casse.
    """
    return mapping.get(normalize_team_name(team_name))


def enrich_teams_with_person_id(
    division_id: str | None = None,
    mapping: dict | None = None,
    path: str | Path = DEFAULT_MAPPING_PATH,
) -> dict[str, int]:
    """Met à jour teams.person_id depuis le mapping.

    Retourne {"mapped": n, "unmapped": n}.
    """
    if mapping is None:
        mapping = load_people_mapping(path)

    with get_conn() as conn:
        query = "SELECT id, name FROM teams"
        params: tuple = ()
        if division_id:
            query += " WHERE division_id=?"
            params = (division_id,)
        teams = conn.execute(query, params).fetchall()

        mapped = unmapped = 0
        unmapped_names: list[str] = []

        for team in teams:
            result = resolve_person(team["name"], mapping)
            if result:
                person_id, _ = result
                conn.execute(
                    "UPDATE teams SET person_id=? WHERE id=?",
                    (person_id, team["id"]),
                )
                mapped += 1
            else:
                unmapped += 1
                unmapped_names.append(team["name"])

    scope = f" [{division_id}]" if division_id else ""
    print(f"[PEOPLE] {mapped} équipes mappées, {unmapped} non mappées{scope}")
    if unmapped_names:
        print(f"[PEOPLE] Non mappées : {unmapped_names}")

    return {"mapped": mapped, "unmapped": unmapped}
