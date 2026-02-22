#!/usr/bin/env python3
"""Régénère les pages HTML statiques depuis la DB.

Usage:
    python generate_pages.py                     # toutes les pages supportées
    python generate_pages.py classement_cumul    # page spécifique
    python generate_pages.py podiums hall_of_fame
"""

import json
import math
import re
import sys
from collections import defaultdict
from pathlib import Path

import yaml

from mpg_db import get_conn
from mpg_legacy_engine import (
    list_included_divisions, fetch_matches, compute_mpg_season_standings,
    compute_streaks,
)
from mpg_people import DEFAULT_MAPPING_PATH

BASE_DIR = Path(__file__).parent

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
PLAYER_INITIALS = {
    "raph":     "SC",
    "nico":     "PU",
    "francois": "CC",
    "damien":   "SM",
    "greg":     "CU",
    "marc":     "FM",
    "pierre":   "LU",
    "manu":     "PP",
}
PLAYER_ORDER = ["raph", "nico", "francois", "damien", "greg", "marc", "pierre", "manu"]


# ── helpers ────────────────────────────────────────────────────────────────────

def _load_display_names() -> dict[str, str]:
    data = yaml.safe_load(DEFAULT_MAPPING_PATH.read_text(encoding="utf-8"))
    return {
        pid: info.get("display_name", pid)
        for pid, info in data.get("persons", {}).items()
    }


def inject_const(html_path: Path, var_name: str, data) -> None:
    """Remplace `const VAR_NAME = <json>;` dans le fichier HTML.

    Utilise json.JSONDecoder.raw_decode pour localiser précisément la valeur
    existante, ce qui est robuste même si plusieurs `const` sont sur la même ligne.
    """
    content = html_path.read_text(encoding="utf-8")
    json_str = json.dumps(data, ensure_ascii=False, separators=(",", ":"))

    pattern = re.compile(rf'const {re.escape(var_name)}\s*=\s*')
    m = pattern.search(content)
    if not m:
        print(f"  ⚠ {html_path.name} : const {var_name} introuvable — fichier non modifié")
        return

    start_val = m.end()
    try:
        _, end_val = json.JSONDecoder().raw_decode(content, start_val)
    except json.JSONDecodeError as e:
        print(f"  ⚠ {html_path.name} : JSON invalide pour {var_name} — {e}")
        return

    new_content = content[:m.start()] + f"const {var_name}={json_str}" + content[end_val:]
    html_path.write_text(new_content, encoding="utf-8")


def _snum_map(conn) -> tuple[dict[str, int], dict[str, int]]:
    """Retourne ({division_id: snum}, {division_id: year}) pour les 18 divisions complètes."""
    divs = list_included_divisions(conn)  # 18 complètes par défaut
    ph = ",".join("?" * len(divs))
    rows = conn.execute(
        f"SELECT division_id, season FROM divisions_metadata "
        f"WHERE division_id IN ({ph}) ORDER BY season, division_id", divs
    ).fetchall()
    snum_map = {r["division_id"]: i + 1 for i, r in enumerate(rows)}
    year_map = {r["division_id"]: r["season"] for r in rows}
    return snum_map, year_map


def _latest_team_names(conn) -> dict[str, str]:
    """Retourne {person_id: team_name} depuis la division complète la plus récente."""
    divs = list_included_divisions(conn)
    if not divs:
        return {}
    ph = ",".join("?" * len(divs))
    rows = conn.execute(
        f"""SELECT t.person_id, t.name, dm.season, dm.division_id
            FROM teams t
            JOIN divisions_metadata dm ON t.division_id = dm.division_id
            WHERE t.division_id IN ({ph}) AND t.person_id IS NOT NULL
            ORDER BY dm.season DESC, dm.division_id DESC""",
        divs,
    ).fetchall()
    seen: dict[str, str] = {}
    for r in rows:
        if r["person_id"] not in seen:
            seen[r["person_id"]] = r["name"]
    return seen


# ── builders classement ────────────────────────────────────────────────────────

def build_classement_raw(conn) -> tuple[dict, dict]:
    """Construit RAW pour classement_cumul et classement_chronologique.
    Inclut toutes les divisions non-COVID (incl. incomplètes + en cours).
    """
    divisions = list_included_divisions(
        conn, include_covid=False, include_incomplete=True, include_current=True,
    )
    ph = ",".join("?" * len(divisions))
    meta_rows = conn.execute(
        f"SELECT division_id, season FROM divisions_metadata "
        f"WHERE division_id IN ({ph}) ORDER BY season, division_id", divisions,
    ).fetchall()
    ordered_divs = [r["division_id"] for r in meta_rows]
    div_season   = {r["division_id"]: r["season"] for r in meta_rows}

    labels:  list[str]  = []
    seasons: list[dict] = []
    for snum, div_id in enumerate(ordered_divs, 1):
        start = (snum - 1) * 14
        for gw in range(1, 15):
            labels.append(f"S{snum}.J{gw}")
        seasons.append({"snum": snum, "year": div_season[div_id],
                        "start": start, "end": start + 13})

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
        return [{"id": pid, "name": display.get(pid, pid),
                 "color": PLAYER_COLORS[pid], "data": data_by_pid[pid]}
                for pid in PLAYER_ORDER]

    return ({"labels": labels, "players": _players(cum_data), "seasons": seasons},
            {"labels": labels, "players": _players(rat_data), "seasons": seasons})


# ── builders podiums ───────────────────────────────────────────────────────────

def build_podiums_data(conn) -> dict:
    """Construit D pour podiums.html — standings par saison complète."""
    snum_map, year_map = _snum_map(conn)
    standings_by_div = compute_mpg_season_standings(conn)

    seasons = []
    for div_id, snum in sorted(snum_map.items(), key=lambda x: x[1]):
        data = standings_by_div.get(div_id)
        if not data:
            continue
        standings = []
        for row in data["standings"]:
            standings.append({
                "pid":         row["person_id"],
                "division_id": div_id,
                "pts":         row["points"],
                "j":           row["matches_played"],
                "v":           row["wins"],
                "n":           row["draws"],
                "d":           row["losses"],
                "bp":          row["goals_for"],
                "bc":          row["goals_against"],
            })
        seasons.append({"snum": snum, "year": year_map[div_id], "standings": standings})

    return {"seasons": seasons}


# ── builders hall_of_fame / hall_of_shame ──────────────────────────────────────

def _build_hall_data(conn) -> tuple[list, list]:
    """Retourne (hof_data, hos_data) pour les deux pages hall."""
    snum_map, year_map = _snum_map(conn)
    standings_by_div  = compute_mpg_season_standings(conn)
    display           = _load_display_names()
    team_names        = _latest_team_names(conn)
    n_seasons         = len(snum_map)

    stats: dict[str, dict] = {
        pid: {
            "titres": 0, "titres_list": [],
            "podiums": 0, "chapeaux": 0, "chapeaux_list": [],
            "seasons": 0,
        }
        for pid in PLAYER_ORDER
    }

    for div_id, snum in sorted(snum_map.items(), key=lambda x: x[1]):
        data = standings_by_div.get(div_id)
        if not data or not data["is_complete"]:
            continue
        year  = year_map[div_id]
        rows  = data["standings"]
        n     = len(rows)
        for i, row in enumerate(rows):
            pid = row["person_id"]
            if pid not in stats:
                continue
            s = stats[pid]
            s["seasons"] += 1
            entry = {"snum": snum, "year": year,
                     "pts": row["points"], "v": row["wins"],
                     "n": row["draws"],   "d": row["losses"]}
            if i == 0:
                s["titres"]  += 1
                s["podiums"] += 1
                s["titres_list"].append(entry)
            elif i < 3:
                s["podiums"] += 1
            if i == n - 1:
                s["chapeaux"] += 1
                s["chapeaux_list"].append(entry)

    def _row(pid, s):
        return {
            "pid":      pid,
            "name":     team_names.get(pid, pid),
            "display":  display.get(pid, pid),
            "initials": PLAYER_INITIALS[pid],
            "color":    PLAYER_COLORS[pid],
            "titres":   s["titres"],
            "titres_list": s["titres_list"],
            "podiums":  s["podiums"],
            "chapeaux": s["chapeaux"],
            "chapeaux_list": s["chapeaux_list"],
            "seasons":  s["seasons"],
            "pct":      round(s["titres"] / s["seasons"] * 100) if s["seasons"] else 0,
        }

    all_rows = [_row(pid, stats[pid]) for pid in PLAYER_ORDER if stats[pid]["seasons"] > 0]
    hof = sorted(all_rows, key=lambda r: (-r["titres"], -r["podiums"], -r["seasons"]))
    hos = sorted(all_rows, key=lambda r: (-r["chapeaux"], r["titres"]))
    return hof, hos, n_seasons


# ── builders records ───────────────────────────────────────────────────────────

def build_records_data(conn) -> dict:
    """Construit ALL_PERF, POS_DIST, RECORDS, TOP10, SAFE_THR, TITLE_THR, SCALE."""
    snum_map, year_map = _snum_map(conn)
    standings_by_div  = compute_mpg_season_standings(conn)
    display           = _load_display_names()

    all_perf = []
    for div_id, snum in sorted(snum_map.items(), key=lambda x: x[1]):
        data = standings_by_div.get(div_id)
        if not data or not data["is_complete"]:
            continue
        year = year_map[div_id]
        rows = data["standings"]
        for pos, row in enumerate(rows, 1):
            pid = row["person_id"]
            all_perf.append({
                "snum":     snum,
                "year":     year,
                "pid":      pid,
                "display":  display.get(pid, pid),
                "color":    PLAYER_COLORS.get(pid, "#888"),
                "initials": PLAYER_INITIALS.get(pid, "??"),
                "pos":      pos,
                "pts":      row["points"],
                "v":        row["wins"],
                "n":        row["draws"],
                "d":        row["losses"],
                "gd":       int(row["goals_for"] - row["goals_against"]),
                "bp":       int(row["goals_for"]),
                "bc":       int(row["goals_against"]),
            })

    # POS_DIST
    by_pos: dict[int, list[int]] = defaultdict(list)
    for p in all_perf:
        by_pos[p["pos"]].append(p["pts"])
    pos_dist = []
    for pos in range(1, 9):
        vals = sorted(by_pos[pos])
        if vals:
            pos_dist.append({
                "pos":    pos,
                "min":    min(vals),
                "max":    max(vals),
                "avg":    round(sum(vals) / len(vals), 1),
                "values": vals,
            })

    # Thresholds
    safe_thr  = max(by_pos[8]) if by_pos[8] else 0
    vals_p1   = sorted(by_pos[1])
    title_thr = vals_p1[len(vals_p1) // 2] if vals_p1 else 0  # médiane basse
    scale     = max(by_pos[1]) + 2 if by_pos[1] else 40

    # TOP10
    top10 = sorted(all_perf, key=lambda p: (-p["pts"], -p["gd"], -p["bp"]))[:10]

    # RECORDS (8 records)
    def _fmt(p):
        return {
            "pid":      p["pid"],
            "display":  p["display"],
            "color":    p["color"],
            "initials": p["initials"],
            "season":   f"S{p['snum']} · {p['year']}",
        }

    def _rec(icon, label, p, value, sub):
        return {**_fmt(p), "icon": icon, "label": label, "value": value, "sub": sub}

    def _sub(p):
        return f"{p['v']}V {p['n']}N {p['d']}D"

    best  = max(all_perf, key=lambda p: (p["pts"], p["gd"]))
    worst = min(all_perf, key=lambda p: (p["pts"], p["gd"]))
    most_w = max(all_perf, key=lambda p: p["v"])
    most_l = max(all_perf, key=lambda p: p["d"])
    best_gd  = max(all_perf, key=lambda p: p["gd"])
    worst_gd = min(all_perf, key=lambda p: p["gd"])
    most_bp  = max(all_perf, key=lambda p: p["bp"])
    most_bc  = max(all_perf, key=lambda p: p["bc"])

    records = [
        _rec("🏅", "Meilleure saison",    best,     f"{best['pts']} pts",           _sub(best)),
        _rec("💀", "Pire saison",          worst,    f"{worst['pts']} pts",          _sub(worst)),
        _rec("🎯", "Plus de victoires",    most_w,   f"{most_w['v']} victoires",     _sub(most_w)),
        _rec("😭", "Plus de défaites",     most_l,   f"{most_l['d']} défaites",      _sub(most_l)),
        _rec("📈", "Meilleure diff buts",  best_gd,  f"+{best_gd['gd']} ({best_gd['bp']}/{best_gd['bc']})",  _sub(best_gd)),
        _rec("📉", "Pire diff buts",       worst_gd, f"{worst_gd['gd']} ({worst_gd['bp']}/{worst_gd['bc']})", _sub(worst_gd)),
        _rec("⚽", "Plus de buts marqués", most_bp,  f"{most_bp['bp']} buts",        _sub(most_bp)),
        _rec("🥅", "Plus de buts encaissés", most_bc, f"{most_bc['bc']} buts",       _sub(most_bc)),
    ]

    return {
        "safe_thr": safe_thr, "title_thr": title_thr, "scale": scale,
        "pos_dist": pos_dist, "records": records, "top10": top10,
        "all_perf": all_perf,
    }


# ── générateurs de pages ───────────────────────────────────────────────────────

def generate_classements() -> None:
    with get_conn() as conn:
        raw_cumul, raw_ratio = build_classement_raw(conn)
    n = len(raw_cumul["labels"])
    inject_const(BASE_DIR / "classement_cumul.html",         "RAW", raw_cumul)
    print(f"  ✓ classement_cumul.html          ({n} labels)")
    inject_const(BASE_DIR / "classement_chronologique.html", "RAW", raw_ratio)
    print(f"  ✓ classement_chronologique.html  ({n} labels)")


def generate_podiums() -> None:
    with get_conn() as conn:
        d = build_podiums_data(conn)
    inject_const(BASE_DIR / "podiums.html", "D", d)
    print(f"  ✓ podiums.html  ({len(d['seasons'])} saisons)")


def generate_halls() -> None:
    with get_conn() as conn:
        hof, hos, n_seasons = _build_hall_data(conn)
    inject_const(BASE_DIR / "hall_of_fame.html",  "DATA",      hof)
    inject_const(BASE_DIR / "hall_of_fame.html",  "N_SEASONS", n_seasons)
    print(f"  ✓ hall_of_fame.html   ({len(hof)} joueurs, {n_seasons} saisons)")
    inject_const(BASE_DIR / "hall_of_shame.html", "DATA",      hos)
    print(f"  ✓ hall_of_shame.html  ({len(hos)} joueurs)")


def generate_records() -> None:
    with get_conn() as conn:
        r = build_records_data(conn)
    html = BASE_DIR / "records.html"
    # SAFE_THR/TITLE_THR/SCALE sont sur la même ligne comme entiers, pas du JSON
    content = html.read_text(encoding="utf-8")
    new_content = re.sub(
        r'const SAFE_THR=\d+,TITLE_THR=\d+,SCALE=\d+',
        f"const SAFE_THR={r['safe_thr']},TITLE_THR={r['title_thr']},SCALE={r['scale']}",
        content,
    )
    html.write_text(new_content, encoding="utf-8")
    inject_const(html, "POS_DIST", r["pos_dist"])
    inject_const(html, "RECORDS",  r["records"])
    inject_const(html, "TOP10",    r["top10"])
    inject_const(html, "ALL_PERF", r["all_perf"])
    print(f"  ✓ records.html  ({len(r['all_perf'])} perf, {len(r['records'])} records)")


# ── builders streaks ───────────────────────────────────────────────────────────

def build_streaks_data(conn) -> list[dict]:
    """Retourne STREAKS trié par best_win DESC pour streaks.html."""
    raw = compute_streaks(conn)
    display = _load_display_names()
    rows = []
    for pid in PLAYER_ORDER:
        s = raw.get(pid)
        if not s:
            continue
        rows.append({
            "pid":            pid,
            "name":           display.get(pid, pid),
            "color":          PLAYER_COLORS[pid],
            "best_win":       s["best_win"],
            "best_unbeaten":  s["best_unbeaten"],
            "best_loss":      s["best_loss"],
            "current_type":   s["current_type"],
            "current_length": s["current_length"],
        })
    return sorted(rows, key=lambda r: (-r["best_win"], -r["best_unbeaten"]))


def generate_streaks() -> None:
    with get_conn() as conn:
        data = build_streaks_data(conn)
    inject_const(BASE_DIR / "streaks.html", "STREAKS", data)
    best = max(data, key=lambda r: r["best_win"])
    print(f"  ✓ streaks.html  (meilleure série V : {best['name']} ({best['best_win']}))")


# ── builders h2h ──────────────────────────────────────────────────────────────

def _h2h_cell_style(w: int, d: int, total: int) -> tuple[str, str]:
    """Returns (bg_color, border_color) rgba strings for a H2H matrix cell."""
    if total == 0 or w == d:
        return "rgba(150,150,150,0.1)", "rgba(150,150,150,0.3)"
    diff = abs(w - d)
    alpha = round(min(0.30, diff / total * 1.5), 2)
    border = round(min(0.70, alpha + 0.30), 2)
    if w > d:
        return f"rgba(69,201,69,{alpha})", f"rgba(69,201,69,{border})"
    return f"rgba(192,48,58,{alpha})", f"rgba(192,48,58,{border})"


def build_h2h_data(conn) -> dict:
    """Compute H2H W/N/D/GD for all player pairs from 18 historical divisions."""
    divs = list_included_divisions(conn)
    matches = fetch_matches(conn, divs)

    h2h: dict[str, dict] = {
        p1: {p2: {"w": 0, "n": 0, "d": 0, "gd": 0.0}
             for p2 in PLAYER_ORDER if p2 != p1}
        for p1 in PLAYER_ORDER
    }

    for m in matches:
        hp, ap = m["home_person_id"], m["away_person_id"]
        if not hp or not ap or hp not in h2h or ap not in h2h:
            continue
        fr = m["final_result"]
        gd = m["home_score"] - m["away_score"]

        h2h[hp][ap]["gd"] += gd
        h2h[ap][hp]["gd"] -= gd

        if fr == 1:
            h2h[hp][ap]["w"] += 1
            h2h[ap][hp]["d"] += 1
        elif fr == 3:
            h2h[ap][hp]["w"] += 1
            h2h[hp][ap]["d"] += 1
        else:
            h2h[hp][ap]["n"] += 1
            h2h[ap][hp]["n"] += 1

    for p1 in PLAYER_ORDER:
        for p2 in PLAYER_ORDER:
            if p2 != p1:
                h2h[p1][p2]["gd"] = round(h2h[p1][p2]["gd"])

    return h2h


def generate_h2h() -> None:
    with get_conn() as conn:
        h2h = build_h2h_data(conn)
    display = _load_display_names()

    def _gd_str(gd: int) -> str:
        return f"+{gd}" if gd >= 0 else str(gd)

    def _best_opponent(pid: str, key: str) -> str:
        """Return opponent with max h2h[pid][opp][key]."""
        return max(
            (q for q in PLAYER_ORDER if q != pid),
            key=lambda q: h2h[pid][q][key],
        )

    # Totals per player
    totals: dict[str, dict] = {}
    for pid in PLAYER_ORDER:
        w = sum(h2h[pid][q]["w"] for q in PLAYER_ORDER if q != pid)
        n = sum(h2h[pid][q]["n"] for q in PLAYER_ORDER if q != pid)
        d = sum(h2h[pid][q]["d"] for q in PLAYER_ORDER if q != pid)
        gd = round(sum(h2h[pid][q]["gd"] for q in PLAYER_ORDER if q != pid))
        totals[pid] = {"w": w, "n": n, "d": d, "gd": gd, "pts": w * 3 + n}

    # Build <main> HTML
    p: list[str] = []
    p.append('<main>\n  <div class="matrix-wrap">\n')
    p.append(
        '    <div class="matrix-title">Matrice H2H — lire par ligne'
        ' (V N D du joueur en ligne contre le joueur en colonne)</div>\n'
    )
    p.append(
        '    <div class="reading-tip">\n'
        '      💡 Chaque case = bilan du joueur en <strong>ligne</strong>'
        ' contre le joueur en <strong>colonne</strong>'
        ' (V = victoire, N = nul, D = défaite) · Différence de buts en dessous\n'
        '    </div>\n'
    )
    p.append(
        '    <div class="legend">\n'
        '      <span class="leg-item"><span class="leg-swatch"'
        ' style="background:rgba(69,201,69,.35);border-color:rgba(69,201,69,.7)">'
        '</span>Domine</span>\n'
        '      <span class="leg-item"><span class="leg-swatch"'
        ' style="background:rgba(150,150,150,.1);border-color:rgba(150,150,150,.3)">'
        '</span>Équilibré</span>\n'
        '      <span class="leg-item"><span class="leg-swatch"'
        ' style="background:rgba(192,48,58,.35);border-color:rgba(192,48,58,.7)">'
        '</span>Dominé</span>\n'
        '    </div>\n'
    )

    # Matrix table
    p.append('    <table>\n')
    hdr = '      <tr><th class="corner">↓ vs →</th>'
    for col in PLAYER_ORDER:
        hdr += (
            f'<th class="col-header" style="--pc:{PLAYER_COLORS[col]}">'
            f'<span class="dot"></span>{display.get(col, col)}</th>'
        )
    hdr += '<th class="col-total">Total</th></tr>\n'
    p.append(hdr)

    for rp in PLAYER_ORDER:
        t = totals[rp]
        rname = display.get(rp, rp)
        row = (
            f'<tr><td class="player-label" style="--pc:{PLAYER_COLORS[rp]}">'
            f'<span class="dot"></span>{rname}</td>'
        )
        for cp in PLAYER_ORDER:
            if cp == rp:
                row += '<td class="diag">—</td>'
            else:
                c = h2h[rp][cp]
                w, n, d, gd = c["w"], c["n"], c["d"], c["gd"]
                tot = w + n + d
                bg, bord = _h2h_cell_style(w, d, tot)
                cname = display.get(cp, cp)
                gds = _gd_str(gd)
                row += (
                    f'<td class="cell" style="background:{bg};border-color:{bord}"'
                    f' title="{rname} vs {cname}: {w}V {n}N {d}D | GD: {gds} | {tot} matchs">'
                    f'<span class="record">{w}V&nbsp;{n}N&nbsp;{d}D</span>'
                    f'<span class="gd">{gds}</span></td>'
                )
        row += (
            f'<td class="total-cell">'
            f'<span class="total-record">{t["w"]}V {t["n"]}N {t["d"]}D</span>'
            f'<span class="total-pts">{t["pts"]} pts · {_gd_str(t["gd"])}</span>'
            f'</td></tr>\n'
        )
        p.append(row)

    p.append('    </table>\n  </div>\n\n')  # end matrix-wrap

    # Summary cards
    p.append(
        '  <div class="summary-section">\n'
        '    <div class="section-title">Résumés individuels</div>\n'
        '    <div class="cards-grid">\n'
    )

    for pid in PLAYER_ORDER:
        t = totals[pid]
        total_m = t["w"] + t["n"] + t["d"]
        pct = round(t["w"] / total_m * 100) if total_m else 0

        nem_pid = _best_opponent(pid, "d")
        vic_pid = _best_opponent(pid, "w")
        # Nemesis display: nemesis's record vs this player
        nem = h2h[nem_pid][pid]
        # Victim display: this player's record vs victim
        vic = h2h[pid][vic_pid]

        p.append(
            f'    <div class="summary-card">\n'
            f'      <div class="sc-header" style="--pc:{PLAYER_COLORS[pid]}">\n'
            f'        <span class="sc-dot"></span>\n'
            f'        <span class="sc-name">{display.get(pid, pid)}</span>\n'
            f'        <span class="sc-record">{t["w"]}V {t["n"]}N {t["d"]}D</span>\n'
            f'      </div>\n'
            f'      <div class="sc-body">\n'
            f'        <div class="sc-row">\n'
            f'          <span class="sc-label">🏆 % victoires</span>\n'
            f'          <span class="sc-val">{pct}%</span>\n'
            f'        </div>\n'
            f'        <div class="sc-row nemesis-row">\n'
            f'          <span class="sc-label">😈 Nemesis</span>\n'
            f'          <span class="sc-val" style="color:{PLAYER_COLORS[nem_pid]}">'
            f'{display.get(nem_pid, nem_pid)}'
            f' <small>({nem["w"]}V {nem["n"]}N {nem["d"]}D)</small></span>\n'
            f'        </div>\n'
            f'        <div class="sc-row victim-row">\n'
            f'          <span class="sc-label">😏 Victime</span>\n'
            f'          <span class="sc-val" style="color:{PLAYER_COLORS[vic_pid]}">'
            f'{display.get(vic_pid, vic_pid)}'
            f' <small>({vic["w"]}V {vic["n"]}N {vic["d"]}D)</small></span>\n'
            f'        </div>\n'
            f'      </div>\n'
            f'    </div>\n'
        )

    p.append('    </div>\n  </div>\n</main>')

    html_path = BASE_DIR / "h2h.html"
    content = html_path.read_text(encoding="utf-8")
    start = content.index('<main>')
    end = content.index('</main>') + len('</main>')
    html_path.write_text(content[:start] + ''.join(p) + content[end:], encoding="utf-8")

    sample = totals[PLAYER_ORDER[0]]
    matches_pp = sample["w"] + sample["n"] + sample["d"]
    print(f"  ✓ h2h.html  ({matches_pp} matchs/joueur, {len(PLAYER_ORDER)} joueurs)")


# ── builders bonus_impact ─────────────────────────────────────────────────────

def build_bonus_usage(conn) -> dict:
    """Returns PLAYER_USAGE {display_name: {bonus_type: count}} from 18 historical divisions."""
    divs = list_included_divisions(conn)
    ph = ",".join("?" * len(divs))

    team_rows = conn.execute(
        f"SELECT id, person_id FROM teams "
        f"WHERE division_id IN ({ph}) AND person_id IS NOT NULL",
        divs,
    ).fetchall()
    team_to_pid = {r["id"]: r["person_id"] for r in team_rows}

    match_rows = conn.execute(
        f"SELECT home_team_id, away_team_id, home_bonuses, away_bonuses "
        f"FROM matches WHERE division_id IN ({ph})",
        divs,
    ).fetchall()

    counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for row in match_rows:
        for team_id, bonuses_json in (
            (row["home_team_id"], row["home_bonuses"]),
            (row["away_team_id"], row["away_bonuses"]),
        ):
            if not team_id or not bonuses_json:
                continue
            pid = team_to_pid.get(team_id)
            if not pid:
                continue
            for bonus_type in json.loads(bonuses_json):
                counts[pid][bonus_type] += 1

    display = _load_display_names()
    return {
        display.get(pid, pid): dict(counts.get(pid, {}))
        for pid in PLAYER_ORDER
    }


def generate_bonus_impact() -> None:
    with get_conn() as conn:
        player_usage = build_bonus_usage(conn)
    html_path = BASE_DIR / "bonus_impact.html"
    inject_const(html_path, "PLAYER_USAGE", player_usage)
    n_total = sum(sum(v.values()) for v in player_usage.values())
    print(f"  ✓ bonus_impact.html  ({n_total} bonus utilisations)")


# ── registre des pages ─────────────────────────────────────────────────────────

PAGES: dict[str, callable] = {
    "classement_cumul":         generate_classements,
    "classement_chronologique": generate_classements,
    "podiums":                  generate_podiums,
    "hall_of_fame":             generate_halls,
    "hall_of_shame":            generate_halls,
    "records":                  generate_records,
    "streaks":                  generate_streaks,
    "h2h":                      generate_h2h,
    "bonus_impact":             generate_bonus_impact,
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
                import traceback
                print(f"  ✗ {t} : {exc}")
                traceback.print_exc()
                errors += 1
    print("✅ Terminé" if not errors else f"⚠ Terminé avec {errors} erreur(s)")


if __name__ == "__main__":
    main()
