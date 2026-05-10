"""backfill_recaps.py — Génère rétroactivement les recaps pour des journées passées.

Utilisation :
  python backfill_recaps.py            # backfill J1-J11 de la division courante
  python backfill_recaps.py 1 5        # backfill J1 à J5
  python backfill_recaps.py --force    # écrase les recaps déjà cachés
"""

from __future__ import annotations

import sys

from event_detector import detect_all_events
from generate_pages import _ensure_recap_table, _load_recap, _save_recap, slabel
from mpg_db import get_conn
from summary_writer import write_summary


def backfill(start_gw: int = 1, end_gw: int = 11, force: bool = False) -> None:
    with get_conn() as conn:
        _ensure_recap_table(conn)
        cur_div_row = conn.execute(
            "SELECT division_id, season FROM divisions_metadata WHERE is_current=1 LIMIT 1"
        ).fetchone()
        if not cur_div_row:
            print("Pas de division courante — abandon.")
            return
        div_id = cur_div_row["division_id"]
        season = cur_div_row["season"]
        sl = slabel(div_id)

        for gw in range(start_gw, end_gw + 1):
            # Vérifier que la journée est bien finalisée
            done = conn.execute(
                "SELECT COUNT(*) AS total, SUM(is_finalized) AS done "
                "FROM matches WHERE division_id=? AND game_week=?",
                (div_id, gw),
            ).fetchone()
            if not done or done["done"] != done["total"] or done["total"] == 0:
                print(f"  J{gw} {sl} : pas finalisée, skip.")
                continue

            cached = None if force else _load_recap(conn, season, div_id, gw)
            if cached:
                print(f"  J{gw} {sl} : déjà en cache (skip — utilise --force pour regénérer).")
                continue

            gw_info = {
                "season":      season,
                "division_id": div_id,
                "game_week":   gw,
                "slabel":      sl,
            }
            detection = detect_all_events(conn, gw_info_override=gw_info)
            if not detection:
                print(f"  J{gw} {sl} : aucun événement.")
                continue

            n_events = len(detection["events"])
            summary = write_summary(detection)
            _save_recap(conn, season, div_id, gw, detection["events"], summary)
            print(f"  J{gw} {sl} : {n_events} événements → \"{summary['title']}\"")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    force_flag = "--force" in sys.argv[1:]
    if len(args) == 0:
        start, end = 1, 11
    elif len(args) == 1:
        start = end = int(args[0])
    elif len(args) == 2:
        start, end = int(args[0]), int(args[1])
    else:
        print(__doc__)
        sys.exit(1)
    print(f"backfill_recaps.py — J{start} à J{end} (force={force_flag})")
    backfill(start, end, force_flag)
    # Régénération de recaps.html pour exposer les nouvelles entrées
    print("\nRégénération de docs/recaps.html...")
    import subprocess
    subprocess.run([sys.executable, "generate_pages.py", "index"], check=True)
