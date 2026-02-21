"""Tests de validation de l'export JSON MPG.

Usage : python test_export.py <path_to_json>
        python test_export.py exports/div_17_1.json
"""

import json
import sys
from pathlib import Path


def run(path: str) -> None:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    errors: list[str] = []

    # ── meta ────────────────────────────────────────────────────────────────
    meta = data.get("meta", {})
    for key in ("exported_at", "division_id", "source", "schema_version"):
        if key not in meta:
            errors.append(f"meta.{key} manquant")

    # ── league ──────────────────────────────────────────────────────────────
    if "league" in data:
        if not isinstance(data["league"], dict) or not data["league"]:
            errors.append("league vide ou mauvais type")

    # ── teams ───────────────────────────────────────────────────────────────
    if "teams" in data:
        teams = data["teams"]
        if len(teams) != 8:
            errors.append(f"teams: attendu 8, obtenu {len(teams)}")
        for t in teams:
            for key in ("id", "name", "players"):
                if key not in t:
                    errors.append(f"teams[{t.get('id','?')}].{key} manquant")

    # ── matches ─────────────────────────────────────────────────────────────
    if "matches" in data:
        matches = data["matches"]
        expected = 56
        gw_min = meta.get("gw_min")
        gw_max = meta.get("gw_max")
        if gw_min is None and gw_max is None and len(matches) != expected:
            errors.append(f"matches: attendu {expected}, obtenu {len(matches)}")

        game_weeks = sorted({m["game_week"] for m in matches})
        if game_weeks:
            print(f"  GW couvertes : {game_weeks[0]}→{game_weeks[-1]} ({len(game_weeks)} journées, {len(matches)} matchs)")

        for m in matches[:3]:  # spot-check 3 premiers
            for key in ("id", "game_week", "home", "away"):
                if key not in m:
                    errors.append(f"match[{m.get('id','?')}].{key} manquant")
            for side in ("home", "away"):
                for subkey in ("team_id", "team_name", "bonuses"):
                    if subkey not in m.get(side, {}):
                        errors.append(f"match.{side}.{subkey} manquant")

    # ── bonus_catalog ───────────────────────────────────────────────────────
    if "bonus_catalog" in data:
        catalog = data["bonus_catalog"]
        if len(catalog) < 11:
            errors.append(f"bonus_catalog: {len(catalog)} entrées (attendu ≥ 11)")
        consumable = [e for e in catalog if e.get("is_consumable")]
        print(f"  Bonus consommables : {[e['ui_label'] for e in consumable]}")
        mcdo = next((e for e in consumable if e["api_key"] == "boostOnePlayer"), None)
        if mcdo and mcdo["stock_default"] != 3:
            errors.append(f"boostOnePlayer stock_default={mcdo['stock_default']} (attendu 3)")

    # ── bonus_remaining ─────────────────────────────────────────────────────
    if "bonus_remaining" in data:
        br = data["bonus_remaining"]
        if len(br) != 8:
            errors.append(f"bonus_remaining: {len(br)} équipes (attendu 8)")
        for team_id, team_data in list(br.items())[:2]:  # spot-check 2 équipes
            bonuses = team_data.get("bonuses", {})
            mcdo = bonuses.get("boostOnePlayer")
            if mcdo is None:
                errors.append(f"bonus_remaining[{team_id}]: boostOnePlayer manquant")
            elif mcdo.get("total") != 3:
                errors.append(f"bonus_remaining[{team_id}].boostOnePlayer.total={mcdo['total']} (attendu 3)")
            else:
                pass  # OK

        # Vérifier cohérence ui_label
        for team_data in br.values():
            for api_key, info in team_data.get("bonuses", {}).items():
                if "ui_label" not in info:
                    errors.append(f"bonus_remaining.bonuses.{api_key}: ui_label manquant")

    # ── Résultat ─────────────────────────────────────────────────────────────
    print(f"\n{'=' * 50}")
    print(f"Fichier : {path}")
    print(f"Division : {meta.get('division_id', '?')}")
    print(f"Exporté le : {meta.get('exported_at', '?')}")
    if errors:
        print(f"\n❌ {len(errors)} erreur(s) :")
        for e in errors:
            print(f"  - {e}")
        sys.exit(1)
    else:
        print("\n✅ Toutes les vérifications passent.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"Usage: python {sys.argv[0]} <export.json>")
        sys.exit(1)
    run(sys.argv[1])
