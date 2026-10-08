"""plan_sync.py — Planifie les syncs du site d'après le calendrier Ligue 1 (un cron par journée).

Lit les dates des matchs de la saison L1 via l'API MPG, et pour chaque journée à venir écrit une
ligne crontab « le lendemain du dernier match, à HH:MM » qui lance `sync_and_publish.sh --light`.
Le bloc est délimité par des marqueurs et réécrit à chaque exécution (les autres lignes du crontab
sont conservées). À relancer quand une journée est décalée.

Usage (venv WSL) :
    python plan_sync.py                 # réécrit le bloc cron pour les journées à venir
    python plan_sync.py --dry-run       # affiche les lignes sans toucher au crontab
    python plan_sync.py --hour 7 --minute 30 --from-gw 6
    python plan_sync.py --remove        # retire le bloc du crontab
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from mpg_client import build_client

BASE = Path(__file__).resolve().parent
PARIS = ZoneInfo("Europe/Paris")
MARK_START = "# >>> mpg-assistant : syncs planifiés d'après le calendrier L1 (plan_sync.py) >>>"
MARK_END = "# <<< mpg-assistant : fin des syncs planifiés <<<"
L1_SEASON = 2026
L1_MAX_GW = 34


def l1_calendar(client, season: int = L1_SEASON) -> list[dict]:
    """[{gw, first, last, n, played}] pour chaque journée L1 connue (dates en heure de Paris)."""
    out = []
    for gw in range(1, L1_MAX_GW + 1):
        r = client.get(f"/championship-matches/1/season/{season}/game-week/{gw}")
        if r.status_code != 200:
            break
        matches = r.json().get("matches") or []
        if not matches:
            break
        dates = []
        for m in matches:
            if m.get("date"):
                dates.append(datetime.fromisoformat(m["date"].replace("Z", "+00:00")).astimezone(PARIS))
        if not dates:
            continue
        out.append({
            "gw": gw, "first": min(dates), "last": max(dates), "n": len(matches),
            "played": sum(1 for m in matches if m.get("period") == "fullTime"),
        })
    return out


def effective_last(j: dict) -> tuple[datetime, bool]:
    """Dernier match réel, ou le dimanche du week-end si les horaires ne sont pas encore fixés
    (la LFP date provisoirement toute la journée au samedi 15:00 tant que la TV n'a pas tranché)."""
    placeholder = j["n"] > 1 and j["first"] == j["last"]
    if placeholder:
        last = j["last"]
        if last.weekday() == 5:          # samedi provisoire → on vise le lundi matin
            last = last + timedelta(days=1)
        return last, True
    return j["last"], False


def cron_lines(cal: list[dict], hour: int, minute: int, from_gw: int, now: datetime) -> list[str]:
    cmd = f"cd {BASE} && /bin/bash {BASE}/sync_and_publish.sh --light"
    lines = []
    for j in cal:
        if j["gw"] < from_gw:
            continue
        last, provisional = effective_last(j)
        run_at = (last + timedelta(days=1)).replace(hour=hour, minute=minute, second=0, microsecond=0)
        if run_at <= now:
            continue
        note = " (horaires provisoires)" if provisional else ""
        lines.append(f"{minute} {hour} {run_at.day} {run_at.month} * {cmd}   "
                     f"# L1 J{j['gw']} : dernier match {last.strftime('%a %d/%m %H:%M')}{note}")
    # Rafraîchissement hebdo du calendrier (jeudi 06:10) : les horaires TV sont fixés quelques semaines à l'avance
    lines.append(f"10 6 * * 4 cd {BASE} && {BASE}/.venv/bin/python3 {BASE}/plan_sync.py --from-gw {from_gw} "
                 f">> {BASE}/sync.log 2>&1   # replanifie d'après le calendrier L1 à jour")
    return lines


def read_crontab() -> str:
    r = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else ""


def write_crontab(content: str) -> None:
    r = subprocess.run(["crontab", "-"], input=content, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"crontab - a échoué : {r.stderr.strip()}")


def replace_block(current: str, lines: list[str]) -> str:
    """Remplace (ou ajoute) le bloc entre marqueurs ; conserve tout le reste."""
    before, after = current, ""
    if MARK_START in current:
        before = current[: current.index(MARK_START)]
        rest = current[current.index(MARK_START):]
        if MARK_END in rest:
            after = rest[rest.index(MARK_END) + len(MARK_END):].lstrip("\n")
    block = ""
    if lines:
        block = MARK_START + "\n" + "\n".join(lines) + "\n" + MARK_END + "\n"
    before = before.rstrip("\n")
    parts = [p for p in (before, block.rstrip("\n"), after.rstrip("\n")) if p]
    return "\n".join(parts) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description="Planifie les syncs d'après le calendrier L1")
    ap.add_argument("--hour", type=int, default=7)
    ap.add_argument("--minute", type=int, default=30)
    ap.add_argument("--from-gw", type=int, default=1, help="première journée L1 à planifier")
    ap.add_argument("--season", type=int, default=L1_SEASON)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--remove", action="store_true", help="retire le bloc planifié du crontab")
    args = ap.parse_args()

    current = read_crontab()
    if args.remove:
        write_crontab(replace_block(current, []))
        print("Bloc retiré du crontab.")
        return

    client, _, _ = build_client()
    cal = l1_calendar(client, args.season)
    if not cal:
        sys.exit("Calendrier L1 vide : token invalide ou saison inconnue.")
    now = datetime.now(PARIS)
    played = [j["gw"] for j in cal if j["played"] == j["n"]]
    print(f"Calendrier L1 {args.season} : {len(cal)} journées connues, {len(played)} jouées (dernière : J{max(played) if played else 0}).")
    lines = cron_lines(cal, args.hour, args.minute, args.from_gw, now)
    print(f"{len(lines)} sync(s) planifié(s) à {args.hour:02d}:{args.minute:02d} le lendemain du dernier match :")
    for l in lines:
        print("  ", l.rsplit("#", 1)[1].strip(), "→", " ".join(l.split()[:5]))
    if args.dry_run:
        print("(dry-run : crontab non modifié)")
        return
    write_crontab(replace_block(current, lines))
    print("✅ crontab mis à jour (bloc entre marqueurs, le reste est conservé). Vérifier : crontab -l")


if __name__ == "__main__":
    main()
