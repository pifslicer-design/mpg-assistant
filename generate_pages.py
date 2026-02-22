#!/usr/bin/env python3
"""Régénère les pages HTML statiques depuis la DB.

Usage:
    python generate_pages.py                     # toutes les pages supportées
    python generate_pages.py classement_cumul    # page spécifique
    python generate_pages.py classement_cumul classement_chronologique
"""

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

import yaml

from mpg_db import get_conn
from mpg_legacy_engine import list_included_divisions, fetch_matches
from mpg_people import DEFAULT_MAPPING_PATH

BASE_DIR = Path(__file__).parent

# Couleurs fixes par joueur (identiques aux pages HTML)
PLAYER_COLORS = {
    "raph":     "#C0303A",
    "nico":     "#2E6A99",
    "francois": "#1E5C3A",
    "damien":   "#C08A00",
    "greg":     "#D4643A",
    "marc":     "#1A3D50",
    "pierre":   "#2A8FA0",
    "manu":     "#7A1A1E",
}

# Ordre d'affichage dans les légendes (identique aux HTML d'origine)
PLAYER_ORDER = ["raph", "nico", "francois", "damien", "greg", "marc", "pierre", "manu"]


# ── helpers ────────────────────────────────────────────────────────────────────

def _load_display_names() -> dict[str, str]:
    data = yaml.safe_load(DEFAULT_MAPPING_PATH.read_text(encoding="utf-8"))
    return {
        pid: info.get("display_name", pid)
        for pid, info in data.get("persons", {}).items()
    }


def inject_const(html_path: Path, var_name: str, data) -> None:
    """Remplace la ligne `const VAR_NAME=...;` dans le fichier HTML."""
    content = html_path.read_text(encoding="utf-8")
    json_str = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    new_line = f"const {var_name}={json_str};"
    new_content, n = re.subn(
        rf"^const {var_name}=.+$",
        new_line,
        content,
        flags=re.MULTILINE,
    )
    if n == 0:
        print(f"  ⚠ {html_path.name} : const {var_name} introuvable — fichier non modifié")
        return
    html_path.write_text(new_content, encoding="utf-8")


# ── builders ───────────────────────────────────────────────────────────────────

def build_classement_raw(conn) -> tuple[dict, dict]:
    """Construit les dicts RAW pour classement_cumul et classement_chronologique.

    Inclut toutes les divisions non-COVID (incomplètes + en cours comprises).
    Retourne (raw_cumul, raw_ratio).
    """
    divisions = list_included_divisions(
        conn,
        include_covid=False,
        include_incomplete=True,
        include_current=True,
    )

    ph = ",".join("?" * len(divisions))
    meta_rows = conn.execute(
        f"SELECT division_id, season FROM divisions_metadata "
        f"WHERE division_id IN ({ph}) ORDER BY season, division_id",
        divisions,
    ).fetchall()
    ordered_divs = [r["division_id"] for r in meta_rows]
    div_season   = {r["division_id"]: r["season"] for r in meta_rows}

    # Labels (S1.J1 … Sn.J14) et métadonnées de saisons pour l'axe
    labels:  list[str]  = []
    seasons: list[dict] = []
    for snum, div_id in enumerate(ordered_divs, 1):
        start = (snum - 1) * 14
        for gw in range(1, 15):
            labels.append(f"S{snum}.J{gw}")
        seasons.append({"snum": snum, "year": div_season[div_id],
                        "start": start, "end": start + 13})

    # Résultats par (division_id, game_week) → {person_id: pts}
    matches = fetch_matches(conn, ordered_divs)
    gw_pts: dict[tuple, dict[str, int]] = defaultdict(dict)
    for m in matches:
        hp, ap = m["home_person_id"], m["away_person_id"]
        if not hp or not ap:
            continue
        fr = m["final_result"]
        if fr == 1:
            gw_pts[(m["division_id"], m["game_week"])][hp] = 3
            gw_pts[(m["division_id"], m["game_week"])][ap] = 0
        elif fr == 2:
            gw_pts[(m["division_id"], m["game_week"])][hp] = 1
            gw_pts[(m["division_id"], m["game_week"])][ap] = 1
        else:
            gw_pts[(m["division_id"], m["game_week"])][hp] = 0
            gw_pts[(m["division_id"], m["game_week"])][ap] = 3

    display = _load_display_names()

    # Accumulation slot par slot
    cumul_pts = {pid: 0 for pid in PLAYER_ORDER}
    cumul_mp  = {pid: 0 for pid in PLAYER_ORDER}
    cum_data  = {pid: [] for pid in PLAYER_ORDER}
    rat_data  = {pid: [] for pid in PLAYER_ORDER}

    for div_id in ordered_divs:
        for gw in range(1, 15):
            key = (div_id, gw)
            for pid in PLAYER_ORDER:
                if key in gw_pts and pid in gw_pts[key]:
                    cumul_pts[pid] += gw_pts[key][pid]
                    cumul_mp[pid]  += 1
                pts = cumul_pts[pid]
                mp  = cumul_mp[pid]
                cum_data[pid].append(pts)
                rat_data[pid].append(round(pts / mp, 4) if mp else 0.0)

    def _players(data_by_pid):
        return [
            {"id": pid, "name": display.get(pid, pid),
             "color": PLAYER_COLORS[pid], "data": data_by_pid[pid]}
            for pid in PLAYER_ORDER
        ]

    raw_cumul = {"labels": labels, "players": _players(cum_data), "seasons": seasons}
    raw_ratio = {"labels": labels, "players": _players(rat_data), "seasons": seasons}
    return raw_cumul, raw_ratio


# ── générateurs de pages ───────────────────────────────────────────────────────

def generate_classements() -> None:
    with get_conn() as conn:
        raw_cumul, raw_ratio = build_classement_raw(conn)

    n = len(raw_cumul["labels"])
    inject_const(BASE_DIR / "classement_cumul.html",         "RAW", raw_cumul)
    print(f"  ✓ classement_cumul.html          ({n} labels)")
    inject_const(BASE_DIR / "classement_chronologique.html", "RAW", raw_ratio)
    print(f"  ✓ classement_chronologique.html  ({n} labels)")


# ── registre des pages ─────────────────────────────────────────────────────────

PAGES: dict[str, callable] = {
    "classement_cumul":         generate_classements,
    "classement_chronologique": generate_classements,
}


def main() -> None:
    targets = sys.argv[1:] or list(PAGES.keys())
    print(f"generate_pages.py — {len(targets)} page(s) demandée(s)")
    done: set = set()
    errors = 0
    for t in targets:
        fn = PAGES.get(t)
        if not fn:
            print(f"  ⚠ Page inconnue : '{t}'  (disponibles : {', '.join(PAGES)})")
            errors += 1
            continue
        if fn not in done:
            try:
                fn()
                done.add(fn)
            except Exception as exc:
                print(f"  ✗ Erreur : {exc}")
                errors += 1
    print("✅ Terminé" if not errors else f"⚠ Terminé avec {errors} erreur(s)")


if __name__ == "__main__":
    main()
