"""MPG API client — authentification + orchestration."""

import argparse
import json
import os
import sys
from collections import defaultdict
from dotenv import load_dotenv
import httpx
import yaml
from mpg_db import (
    init_db, get_conn, get_last_fetched_game_week, get_league_current_game_week,
    mark_finalized_up_to, get_manifest, set_manifest, refresh_divisions_metadata,
)
from mpg_fetchers import fetch_league, fetch_teams, fetch_matches
from mpg_people import DEFAULT_MAPPING_PATH
from mpg_bonuses import print_bonus_report
from mpg_export import build_export, write_export, SCOPES
from mpg_stats import print_stats_report
from mpg_legacy_engine import (
    print_palmares_report, print_elo_report, print_h2h_report,
    print_mpg_season_report, print_streaks_report, resolve_person_id,
)

load_dotenv()

BASE_URL = "https://api.mpg.football"
TOTAL_GAME_WEEKS = 14


def _get_env(key: str) -> str:
    value = os.getenv(key)
    if not value:
        raise ValueError(f"Variable manquante dans .env : {key}")
    return value


def build_client() -> tuple[httpx.Client, str, str]:
    """Retourne (client httpx authentifié, league_id, division_id)."""
    token = _get_env("MPG_TOKEN")
    league_id = _get_env("LEAGUE_ID")
    division_id = _get_env("DIVISION_ID")

    client = httpx.Client(
        base_url=BASE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        timeout=15.0,
    )
    return client, league_id, division_id


def _resolve_current_gw(last_db: int) -> tuple[int, str]:
    """current_gw depuis league, avec fallback sur MAX(matches) si NULL/invalide."""
    from_league = get_league_current_game_week()  # retourne 1 si NULL
    if from_league > 1:
        return from_league, "league"
    if last_db > 1:
        return last_db, "matches_fallback"
    return 1, "default"


def _compute_match_range(force: bool, division_id: str) -> tuple[int, int]:
    """Source de vérité : SELECT MAX(game_week) FROM matches WHERE division_id=?
    Rolling window fixe : toujours refetch current_gw-1 et current_gw.
    """
    manifest_key = f"last_non_empty_gw_saved::{division_id}"
    last_db = get_last_fetched_game_week(division_id)
    current, source = _resolve_current_gw(last_db)
    last_manifest = get_manifest(manifest_key) or "—"

    print(f"[DELTA] current_gw={current}({source}) last_saved_gw_db={last_db}")
    print(f"[MANIFEST] key={manifest_key} value={last_manifest}")

    # Resync manifest si incohérent avec la DB
    if last_db > 0 and str(last_db) != last_manifest:
        set_manifest(manifest_key, str(last_db))
        print(f"[MANIFEST] Resynchronisé : {last_manifest} → {last_db}")

    if force:
        return 1, TOTAL_GAME_WEEKS

    if last_db == 0:
        return 1, current

    # Rolling window fixe : refetch toujours les 2 dernières GW
    from_gw = max(1, current - 1)
    return from_gw, current


def _run_doctor() -> None:
    """Affiche un diagnostic : distribution divisions, dernier fetch, bonus keys."""
    with get_conn() as conn:
        print("\n=== Doctor ===")

        rows = conn.execute("""
            SELECT division_id, COUNT(*) AS cnt, MAX(fetched_at) AS last_fetch
            FROM matches GROUP BY division_id
        """).fetchall()
        print("Matches par division :")
        for r in rows:
            print(f"  {r['division_id'] or '(null)'}: {r['cnt']} matchs, dernier fetch={r['last_fetch']}")

        rows2 = conn.execute(
            "SELECT division_id, home_bonuses, away_bonuses FROM matches WHERE division_id IS NOT NULL"
        ).fetchall()
        by_div: dict = defaultdict(lambda: defaultdict(int))
        for r in rows2:
            for bj in (r["home_bonuses"], r["away_bonuses"]):
                if bj:
                    for k in json.loads(bj):
                        by_div[r["division_id"]][k] += 1

        # divisions_metadata
        meta_rows = conn.execute("""
            SELECT division_id, season, is_covid, is_incomplete, n_matches, gw_min, gw_max
            FROM divisions_metadata
            ORDER BY season, division_id
        """).fetchall()
        if meta_rows:
            print("\nDivisions metadata :")
            print(f"  {'division_id':<40} {'saison':>6} {'matchs':>6} {'GW':>8}  flags")
            for r in meta_rows:
                gw_range = f"{r['gw_min']}-{r['gw_max']}"
                flags = []
                if r["is_covid"]:
                    flags.append("COVID")
                if r["is_incomplete"]:
                    flags.append("incompl.")
                flag_str = ",".join(flags) if flags else "—"
                print(
                    f"  {r['division_id']:<40} {str(r['season'] or '?'):>6} "
                    f"{r['n_matches']:>6} {gw_range:>8}  {flag_str}"
                )

        from bonus_catalog import CONSUMABLE_KEYS, format_bonus_name
        print("\nBonus consommables par division :")
        for div, counts in sorted(by_div.items()):
            consumable = {format_bonus_name(k): v for k, v in counts.items() if k in CONSUMABLE_KEYS}
            print(f"  {div}: {consumable}")
        print()


def _sync_division(
    client: httpx.Client,
    league_id: str,
    division_id: str,
    force: bool = False,
) -> int:
    """Fetch league + teams + matches pour une division. Retourne nb matchs fetchés."""
    fetch_league(client, league_id)
    fetch_teams(client, division_id)

    from_gw, to_gw = _compute_match_range(force, division_id)
    fetched = 0
    if from_gw > to_gw:
        print("[OK] Matches déjà à jour")
    else:
        flag = " (force)" if force else ""
        print(f"[FETCH] Matches GW{from_gw}→GW{to_gw}{flag}")
        fetched = fetch_matches(client, division_id, from_gw, to_gw)

    last_db = get_last_fetched_game_week(division_id)
    current_gw, _ = _resolve_current_gw(last_db)
    finalized_up_to = current_gw - 2
    if finalized_up_to >= 1:
        mark_finalized_up_to(finalized_up_to, division_id)

    refresh_divisions_metadata()

    return fetched


def _print_results(division_id: str, gw: int | None = None) -> None:
    """Affiche les résultats scorés de la division, groupés par journée."""
    data = yaml.safe_load(DEFAULT_MAPPING_PATH.read_text(encoding="utf-8"))
    display = {
        pid: info.get("display_name", pid)
        for pid, info in data.get("persons", {}).items()
    }

    clause = "m.division_id = ? AND m.home_score IS NOT NULL"
    params: list = [division_id]
    if gw is not None:
        clause += " AND m.game_week = ?"
        params.append(gw)

    with get_conn() as conn:
        rows = conn.execute(f"""
            SELECT m.game_week,
                   ht.person_id AS home_pid, ht.name AS home_name,
                   m.home_score, m.away_score,
                   at.person_id AS away_pid, at.name AS away_name
            FROM matches m
            JOIN teams ht ON m.home_team_id = ht.id
            JOIN teams at ON m.away_team_id = at.id
            WHERE {clause}
            ORDER BY m.game_week, m.id
        """, params).fetchall()

    if not rows:
        label = f"J{gw}" if gw else "la saison"
        print(f"[RESULTS] Aucun résultat pour {label}.")
        return

    current_gw = None
    for r in rows:
        if r["game_week"] != current_gw:
            current_gw = r["game_week"]
            print(f"\n── J{current_gw} ──")
        hs, as_ = float(r["home_score"]), float(r["away_score"])
        winner = "←" if hs > as_ else ("→" if as_ > hs else "=")
        home = display.get(r["home_pid"], r["home_name"] or "?")
        away = display.get(r["away_pid"], r["away_name"] or "?")
        print(f"  {home:<12}  {hs:.0f} – {as_:.0f}  {away:<12}  {winner}")
    print()


def _apply_people_mapping(division_id: str | None = None) -> None:
    """Applique people_mapping.yaml si le fichier existe."""
    from pathlib import Path as _P
    mapping_path = _P(__file__).parent / "people_mapping.yaml"
    if not mapping_path.exists():
        return
    from mpg_people import load_people_mapping, enrich_teams_with_person_id
    mapping = load_people_mapping(mapping_path)
    enrich_teams_with_person_id(division_id=division_id, mapping=mapping)


def _check_unmapped_teams(
    include_covid: bool = False,
    include_incomplete: bool = False,
    allow_unmapped: bool = False,
) -> None:
    """Vérifie qu'aucune équipe des divisions incluses n'a person_id IS NULL.

    Par défaut (allow_unmapped=False) : exit(2) si des équipes sont non mappées.
    Avec allow_unmapped=True : avertissement seulement, on continue.
    """
    clauses = ["1=1"]
    if not include_covid:
        clauses.append("dm.is_covid=0")
    if not include_incomplete:
        clauses.append("dm.is_incomplete=0")
    where = " AND ".join(clauses)

    with get_conn() as conn:
        rows = conn.execute(f"""
            SELECT t.division_id, t.name
            FROM teams t
            JOIN divisions_metadata dm ON t.division_id = dm.division_id
            WHERE t.person_id IS NULL AND {where}
            ORDER BY t.division_id, t.name
        """).fetchall()

    if not rows:
        return

    by_div: dict[str, list[str]] = {}
    for r in rows:
        by_div.setdefault(r["division_id"], []).append(r["name"])

    lines = []
    for div, names in sorted(by_div.items()):
        lines.append(f"  {div} → {names}")
    body = "\n".join(lines)
    hint = "  → Corriger people_mapping.yaml ou lancer avec --allow-unmapped."

    if allow_unmapped:
        print(f"[WARNING] Équipes sans person_id (divisions incluses) :\n{body}\n{hint}")
    else:
        print(f"[ERROR] Équipes sans person_id (divisions incluses) :\n{body}\n{hint}")
        sys.exit(2)


def _run_batch(
    args,
    client: httpx.Client,
    league_id: str,
    default_division_id: str,
) -> None:
    """Boucle --force sur toutes les divisions du fichier texte."""
    from pathlib import Path as _P
    lines = _P(args.divisions_file).read_text(encoding="utf-8").strip().splitlines()
    divisions = [l.strip() for l in lines if l.strip() and not l.startswith("#")]

    batch_label = args.league_batch_name or args.divisions_file
    print(f"\n[BATCH] {batch_label} — {len(divisions)} division(s)")

    results: list[dict] = []
    for div_id in divisions:
        print(f"\n[BATCH] ── {div_id} ──")
        try:
            fetched = _sync_division(client, league_id, div_id, force=True)
            _apply_people_mapping(div_id)
            results.append({"division": div_id, "status": "ok", "fetched": fetched})
        except Exception as exc:
            print(f"[ERROR] {div_id} : {exc}")
            results.append({"division": div_id, "status": "error", "error": str(exc)})

    ok  = [r for r in results if r["status"] == "ok"]
    total_fetched = sum(r.get("fetched", 0) for r in ok)
    print(f"\n[BATCH] Résumé : {len(ok)}/{len(results)} OK — {total_fetched} matchs fetchés")
    for r in results:
        status = "✓" if r["status"] == "ok" else "✗"
        detail = f"{r.get('fetched', 0)} matchs" if r["status"] == "ok" else r.get("error", "")
        print(f"  {status} {r['division']} — {detail}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="MPG data fetcher")
    # Ciblage division
    parser.add_argument("--division",          default=None,  help="Division ID (override .env)")
    parser.add_argument("--divisions-file",    default=None,  metavar="PATH",
                        help="Fichier texte : 1 division_id par ligne")
    parser.add_argument("--sync-divisions",    action="store_true",
                        help="Boucle --force sur chaque division du fichier")
    parser.add_argument("--league-batch-name", default=None,  metavar="NAME",
                        help="Label optionnel pour les logs du batch")
    parser.add_argument("--no-fetch",          action="store_true",
                        help="Ne pas appeler l'API (normalisation / export uniquement)")
    # Fetch
    parser.add_argument("--force",             action="store_true", help="Refetch toutes les GW")
    # Affichage
    parser.add_argument("--bonuses",           action="store_true", help="Rapport bonus restants")
    parser.add_argument("--stats",             action="store_true", help="Afficher classement stats")
    parser.add_argument("--include-covid",      action="store_true",
                        help="Inclure les divisions COVID dans les stats")
    parser.add_argument("--include-incomplete", action="store_true",
                        help="Inclure les divisions incomplètes dans les stats")
    parser.add_argument("--doctor",            action="store_true", help="Diagnostic DB")
    parser.add_argument("--allow-unmapped",    action="store_true",
                        help="Avertir (pas d'erreur) si des équipes n'ont pas de person_id")
    # Legacy analytics
    parser.add_argument("--legacy",  action="store_true",
                        help="Rapport palmarès all-time + classement ELO")
    parser.add_argument("--elo",     action="store_true",
                        help="Classement ELO seul")
    parser.add_argument("--h2h",       nargs=2, metavar=("PERSON_A", "PERSON_B"),
                        help="Stats head-to-head (ex: --h2h raph manu)")
    parser.add_argument("--streaks",  action="store_true",
                        help="Séries V/N/D all-time par joueur")
    parser.add_argument("--results", nargs="?", const=0, default=None, type=int,
                        metavar="JW",
                        help="Résultats saison en cours (ex: --results ou --results 3)")
    parser.add_argument("--season-mpg", default=None, metavar="DIVISION_ID",
                        help="Classement J/V/N/D/Pts d'une division MPG")
    # Export
    parser.add_argument("--export",            default=None,  metavar="PATH",
                        help="Exporter en JSON vers PATH")
    parser.add_argument("--export-scope",      default="all", choices=SCOPES,
                        help="Sections à exporter (default: all)")
    parser.add_argument("--export-gw-min",     default=None,  type=int, metavar="N")
    parser.add_argument("--export-gw-max",     default=None,  type=int, metavar="N")
    parser.add_argument("--pretty",            action="store_true", help="JSON indenté")
    args = parser.parse_args()

    init_db()
    client, league_id, default_division_id = build_client()

    # ── Résolution division(s) ──────────────────────────────────────────────
    if args.sync_divisions and args.divisions_file:
        with client:
            _run_batch(args, client, league_id, default_division_id)
        sys.exit(0)

    division_id_effective = args.division or default_division_id
    source = "cli" if args.division else "default"
    print(f"[CTX] division_id={division_id_effective} (source={source})")

    # ── Fetch (sauf --no-fetch) ─────────────────────────────────────────────
    if not args.no_fetch:
        with client:
            _sync_division(client, league_id, division_id_effective, force=args.force)

    # ── Post-sync : normalisation identités (toutes les équipes, idempotent) ──
    # On applique sur l'ensemble des équipes (pas seulement la division courante)
    # pour maintenir la cohérence de l'historique multi-divisions.
    _apply_people_mapping()

    # ── Vérification intégrité du mapping ──────────────────────────────────
    _check_unmapped_teams(
        include_covid=args.include_covid,
        include_incomplete=args.include_incomplete,
        allow_unmapped=args.allow_unmapped,
    )

    # ── Sorties ────────────────────────────────────────────────────────────
    if args.bonuses:
        print_bonus_report(division_id=division_id_effective)

    if args.stats:
        print_stats_report(
            division_id=division_id_effective,
            include_covid=args.include_covid,
            include_incomplete=args.include_incomplete,
        )

    if args.doctor:
        _run_doctor()

    if args.export:
        data = build_export(
            division_id=division_id_effective,
            scope=args.export_scope,
            gw_min=args.export_gw_min,
            gw_max=args.export_gw_max,
        )
        write_export(data, args.export, pretty=args.pretty)

    # ── Legacy analytics (multi-divisions, all-time) ────────────────────────
    if args.legacy:
        print_palmares_report(
            include_covid=args.include_covid,
            include_incomplete=args.include_incomplete,
        )
        print_elo_report(
            include_covid=args.include_covid,
            include_incomplete=args.include_incomplete,
        )

    if args.elo and not args.legacy:
        print_elo_report(
            include_covid=args.include_covid,
            include_incomplete=args.include_incomplete,
        )

    if args.h2h:
        pid_a = resolve_person_id(args.h2h[0])
        pid_b = resolve_person_id(args.h2h[1])
        missing = [n for n, p in zip(args.h2h, [pid_a, pid_b]) if not p]
        if missing:
            print(f"[H2H] Joueur(s) introuvable(s) : {missing}")
        else:
            print_h2h_report(
                pid_a, pid_b,
                include_covid=args.include_covid,
                include_incomplete=args.include_incomplete,
            )

    if args.streaks:
        print_streaks_report(
            include_covid=args.include_covid,
            include_incomplete=args.include_incomplete,
        )

    if args.season_mpg:
        print_mpg_season_report(args.season_mpg)

    if args.results is not None:
        _print_results(division_id_effective, gw=args.results if args.results else None)
