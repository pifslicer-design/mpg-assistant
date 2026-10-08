#!/usr/bin/env python3
"""bestteam_engine.py — Best Team statique : compo optimale de chaque manager, calculée en local.

Remplace l'Edge Function Supabase (supabase/functions/best-team/index.ts, projet Supabase mort) :
même scoring (moyenne pondérée decay 0.85 sur les 10 derniers matchs, bonus buts/passes,
bonus domicile), mêmes formations, même choix de capitaine, même commentaire Claude Haiku.

Sources (tout en local, hors API MPG pour les notes et Anthropic pour le commentaire) :
  - mpg.db                           : effectifs (teams.raw_json.squad), prochain match MPG (matches),
                                       noms de repli (matches.raw_json)
  - mercato_cache/ratings_<saison>.json : notes L1 match par match (format de mercato.py) ; refetch
                                       incrémental via /championship-matches/1/season/{s}/game-week/{gw}
                                       + /championship-match/{id} si un token MPG valide est dans .env
  - mercato_cache/index.json, pool.json, clubs.json : identité des joueurs, cotes, clubs
  - mercato_cache/l1_fixtures.json   : affiche de la prochaine journée L1 (domicile / extérieur)
  - mercato_cache/bestteam_commentary.json : cache des commentaires IA (clé = compo + adversaire)

Usage (debug) :
    python bestteam_engine.py                      # compo des 8 managers de la division courante
    python bestteam_engine.py --division mpg_division_QU0SUZ6HQPB_18_1 --no-fetch --no-ai
    python bestteam_engine.py --json /tmp/bestteam.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).parent
CACHE = BASE / "mercato_cache"
DB_PATH = BASE / "mpg.db"

L1_SEASON = 2026          # ← saison L1 en cours (à changer chaque été, cf. PROJECT_STATE.md)
L1_MAX_GW = 34
WINDOW = 10               # fenêtre glissante : nb de matchs L1 pris en compte
DECAY = 0.85
HOME_BONUS = 0.15
AI_MODEL = "claude-haiku-4-5-20251001"
PROMPT_VERSION = "v2"     # à incrémenter quand le prompt change (invalide le cache des commentaires)

FORMATIONS: dict[str, dict[str, int]] = {
    "3-4-3": {"G": 1, "D": 3, "M": 4, "A": 3},
    "3-5-2": {"G": 1, "D": 3, "M": 5, "A": 2},
    "4-3-3": {"G": 1, "D": 4, "M": 3, "A": 3},
    "4-4-2": {"G": 1, "D": 4, "M": 4, "A": 2},
    "4-5-1": {"G": 1, "D": 4, "M": 5, "A": 1},
    "5-3-2": {"G": 1, "D": 5, "M": 3, "A": 2},
    "5-4-1": {"G": 1, "D": 5, "M": 4, "A": 1},
}
POS_LABEL = {1: "G", 2: "D", 3: "M", 4: "A"}
POS_ORDER = {"G": 0, "D": 1, "M": 2, "A": 3}
CAPTAIN_PREF = {"A": 0, "M": 1, "D": 2, "G": 3}
POS_FR = {"G": "Gardien", "D": "Défenseur", "M": "Milieu", "A": "Attaquant"}
FLAG_VERY_LIMITED = "⚠️ données limitées"   # < 3 matchs : score forcé à 0 (comme l'Edge Function)
FLAG_LIMITED = "données limitées"            # 3 à 5 matchs


# ── Utilitaires ──────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load(name: str, default=None):
    p = CACHE / name
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def _save(name: str, obj) -> None:
    CACHE.mkdir(exist_ok=True)
    (CACHE / name).write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")


def _slabel(div_id: str) -> str:
    n = div_id.split("_")[-2]
    return f"Old S{n}" if "PWN77AILXZQ" in div_id else f"S{n}"


def _pos_label(position=None, ultra=None) -> str:
    if position in POS_LABEL:
        return POS_LABEL[position]
    if ultra is not None:
        try:
            u = int(ultra)
        except (TypeError, ValueError):
            return "M"
        if u < 20:
            return "G"
        if u < 30:
            return "D"
        if u < 40:
            return "M"
        return "A"
    return "M"


def _club_name(clubs: dict, club_id: str | None) -> str:
    c = clubs.get(club_id or "") or {}
    n = c.get("name")
    if isinstance(n, dict):
        n = n.get("fr-FR") or n.get("en-GB") or next(iter(n.values()), None)
    return n or c.get("shortName") or (club_id or "?").replace("mpg_championship_club_", "club ")


def _short_club(clubs: dict, club_id: str | None) -> str:
    return (clubs.get(club_id or "") or {}).get("shortName") or _club_name(clubs, club_id)


def _load_env() -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv(BASE / ".env")
    except Exception:
        pass


def _display_names() -> dict[str, str]:
    try:
        import yaml
        from mpg_people import DEFAULT_MAPPING_PATH
        data = yaml.safe_load(Path(DEFAULT_MAPPING_PATH).read_text(encoding="utf-8"))
        return {pid: info.get("display_name", pid) for pid, info in (data.get("persons") or {}).items()}
    except Exception:
        return {}


# ── Base locale ──────────────────────────────────────────────────────────────

def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _has_teams(conn, div: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM teams WHERE division_id=? AND person_id IS NOT NULL LIMIT 1", (div,)
    ).fetchone() is not None


MIN_SQUAD_FOR_BESTTEAM = 11   # une compo complète exige 11 joueurs par équipe


def _squads_complete(conn, div: str) -> bool:
    """Vrai si chaque équipe (avec person_id) de la division a au moins 11 joueurs (mercato terminé)."""
    rows = conn.execute(
        "SELECT raw_json FROM teams WHERE division_id=? AND person_id IS NOT NULL", (div,)
    ).fetchall()
    if not rows:
        return False
    for r in rows:
        try:
            squad = (json.loads(r[0] or "{}") or {}).get("squad") or {}
        except (TypeError, ValueError):
            squad = {}
        if len(squad) < MIN_SQUAD_FOR_BESTTEAM:
            return False
    return True


def _latest_complete_division(conn) -> str | None:
    """Division la plus récente dont tous les effectifs sont complets (ordre numérique du suffixe)."""
    rows = conn.execute(
        "SELECT DISTINCT division_id FROM teams WHERE person_id IS NOT NULL"
    ).fetchall()
    def key(div):
        parts = div.split("_")
        try:
            return (parts[-3] != "PWN77AILXZQ", int(parts[-2]), int(parts[-1]))
        except (ValueError, IndexError):
            return (True, 0, 0)
    for div in sorted((r[0] for r in rows), key=key, reverse=True):
        if _squads_complete(conn, div):
            return div
    return None


def pick_division(conn, override: str | None = None) -> str:
    """Division à traiter : override CLI → division courante si ses effectifs sont complets (mercato
    terminé) → sinon la dernière division aux effectifs complets (pendant un mercato, Best Team reste
    sur la saison précédente) → dernière synchro."""
    if override:
        return override
    current = None
    try:
        from mpg_db import get_current_division   # dérivée de l'API ligue (table league)
        current = get_current_division()[0]
    except Exception:
        try:
            from mpg_db import CURRENT_DIVISION
            current = CURRENT_DIVISION
        except Exception:
            current = None
    if current and _squads_complete(conn, current):
        return current
    latest = _latest_complete_division(conn)
    if latest:
        if current and _has_teams(conn, current):
            print(f"  ℹ Best Team : effectifs de {current} incomplets (mercato en cours), page calculée sur {latest}")
        return latest
    row = conn.execute("SELECT division_id FROM divisions_metadata WHERE is_current=1 LIMIT 1").fetchone()
    if row and _has_teams(conn, row[0]):
        return row[0]
    row = conn.execute(
        "SELECT division_id FROM teams WHERE person_id IS NOT NULL ORDER BY fetched_at DESC LIMIT 1"
    ).fetchone()
    if not row:
        raise RuntimeError("Aucune équipe en base (lancer un sync mpg_client.py d'abord)")
    return row[0]


def load_teams(conn, div: str) -> list[dict]:
    """Effectif de chaque manager : teams.raw_json.squad, sinon la table players."""
    rows = conn.execute(
        "SELECT id, person_id, name, budget, raw_json FROM teams "
        "WHERE division_id=? AND person_id IS NOT NULL ORDER BY person_id", (div,)
    ).fetchall()
    teams = []
    for r in rows:
        try:
            rj = json.loads(r["raw_json"] or "{}")
        except Exception:
            rj = {}
        squad = rj.get("squad") or {}
        if isinstance(squad, list):
            squad = {p.get("id"): p for p in squad if isinstance(p, dict) and p.get("id")}
        if not squad:
            squad = {
                p["id"]: {"id": p["id"], "price": p["price"], "status": p["status"]}
                for p in conn.execute("SELECT id, price, status FROM players WHERE team_id=?", (r["id"],))
            }
        teams.append({"team_id": r["id"], "person_id": r["person_id"], "name": r["name"],
                      "budget": r["budget"], "squad": squad})
    return teams


def names_from_db(conn, div: str) -> dict[str, dict]:
    """Repli identité joueurs depuis les compos MPG stockées (division courante + précédente)."""
    divs = [r[0] for r in conn.execute(
        "SELECT division_id FROM divisions_metadata ORDER BY season DESC, division_id DESC"
    ).fetchall()]
    scan = [div] + [d for d in divs if d != div][:1]
    out: dict[str, dict] = {}
    for d in scan:
        for (raw,) in conn.execute(
            "SELECT raw_json FROM matches WHERE division_id=? AND raw_json LIKE '%lastName%' "
            "ORDER BY game_week DESC", (d,)
        ):
            try:
                rj = json.loads(raw)
            except Exception:
                continue
            for side in ("home", "away"):
                for pid, p in ((rj.get(side) or {}).get("players") or {}).items():
                    if pid in out or not isinstance(p, dict):
                        continue
                    out[pid] = {"first": p.get("firstName") or "", "last": p.get("lastName") or "",
                                "pos": _pos_label(p.get("position"), p.get("ultraPosition")),
                                "club_id": p.get("clubId")}
    return out


def next_mpg_matches(conn, div: str) -> tuple[dict[str, dict], bool]:
    """{person_id: {game_week, opponent, opponent_id, home}} pour la prochaine journée MPG non jouée.
    Second élément : True si la saison est terminée (des matchs existent et tous sont finalisés)."""
    rows = conn.execute(
        """SELECT m.game_week, h.person_id AS hp, h.name AS hn, a.person_id AS ap, a.name AS an
           FROM matches m
           JOIN teams h ON m.home_team_id = h.id
           JOIN teams a ON m.away_team_id = a.id
           WHERE m.division_id = ?
             AND (m.home_score IS NULL OR COALESCE(m.is_finalized, 0) = 0)
           ORDER BY m.game_week""", (div,)
    ).fetchall()
    n_total = conn.execute("SELECT COUNT(*) FROM matches WHERE division_id=?", (div,)).fetchone()[0]
    if not rows:
        return {}, n_total > 0
    gw = rows[0]["game_week"]
    out: dict[str, dict] = {}
    for r in rows:
        if r["game_week"] != gw:
            break
        if r["hp"]:
            out[r["hp"]] = {"game_week": gw, "opponent": r["an"], "opponent_id": r["ap"], "home": True}
        if r["ap"]:
            out[r["ap"]] = {"game_week": gw, "opponent": r["hn"], "opponent_id": r["hp"], "home": False}
    return out, False


# ── Cache mercato (identités, clubs) ─────────────────────────────────────────

def load_player_index() -> tuple[dict[str, dict], dict[str, dict]]:
    """(index {pid: identité}, stats {pid: stats API}) depuis index.json + pool.json."""
    idx: dict[str, dict] = dict(_load("index.json", {}) or {})
    pool = _load("pool.json", {}) or {}
    stats_by: dict[str, dict] = {}
    for key in ("availablePlayers", "unavailablePlayers"):
        for p in pool.get(key) or []:
            pid = p.get("id")
            if not pid:
                continue
            idx.setdefault(pid, {k: p.get(k) for k in
                                 ("id", "firstName", "lastName", "position", "ultraPosition", "quotation", "clubId")})
            stats_by[pid] = p.get("stats") or {}
    return idx, stats_by


# ── Notes L1 (format mercato.py) ─────────────────────────────────────────────

def _ingest_match(data: dict, md: dict, gw: int) -> int:
    """Port de mercato._fetch_ratings : une ligne par joueur ayant joué (ou noté) dans ce match."""
    n = 0
    for side in ("home", "away"):
        team = md.get(side) or {}
        opp = md.get("away" if side == "home" else "home") or {}
        raw = team.get("players") or {}
        plist = list(raw.values()) if isinstance(raw, dict) else raw
        for p in plist:
            if not isinstance(p, dict):
                continue
            pid = p.get("id") or p.get("playerId")
            if not pid:
                continue
            st = p.get("stats") or {}
            minutes = st.get("minutes_played") or 0
            rating = p.get("mpgRating")
            if not minutes and not rating:
                continue
            entry = {
                "gw": gw, "rating": rating, "goals": st.get("goals") or 0,
                "assists": st.get("goal_assist_intentional") or 0, "minutes": minutes,
                "sub": p.get("sub") or 0, "home": side == "home",
                "club": team.get("clubId"), "opp": opp.get("clubId"),
                "own_goals": st.get("own_goals") or 0, "red": st.get("red_card") or 0,
            }
            lst = [e for e in data["players"].get(pid, []) if e.get("gw") != gw]
            lst.append(entry)
            data["players"][pid] = lst
            n += 1
    return n


def refresh_ratings(client, season: int, max_gw: int = L1_MAX_GW, log=print) -> tuple[dict, dict | None]:
    """Refetch incrémental des notes (les journées complètes en cache ne sont pas refetchées).
    Retourne (ratings, fixtures de la prochaine journée L1 non terminée)."""
    cache_name = f"ratings_{season}.json"
    data = _load(cache_name) or {"season": season, "gws": {}, "players": {}}
    gws = data.setdefault("gws", {})
    data.setdefault("players", {})
    fixtures = None
    for gw in range(1, max_gw + 1):
        if str(gw) in gws and gws[str(gw)].get("complete"):
            continue
        r = client.get(f"/championship-matches/1/season/{season}/game-week/{gw}")
        if r.status_code in (401, 403):
            raise PermissionError(f"API MPG {r.status_code} — token expiré ?")
        if r.status_code != 200:
            break
        matches = r.json().get("matches") or []
        if not matches:
            break
        done = [m for m in matches if m.get("period") == "fullTime"]
        if len(done) < len(matches) and fixtures is None:
            fixtures = {
                "season": season, "gw": gw, "fetched_at": _now_iso(),
                "matches": [{"id": m.get("id"), "home": (m.get("home") or {}).get("clubId"),
                             "away": (m.get("away") or {}).get("clubId"),
                             "date": m.get("date"), "period": m.get("period")} for m in matches],
            }
        if not done:
            break
        n_rows = 0
        for m in done:
            md = client.get(f"/championship-match/{m.get('id')}")
            if md.status_code != 200:
                log(f"    ⚠ match {m.get('id')} → {md.status_code}")
                continue
            n_rows += _ingest_match(data, md.json(), gw)
        gws[str(gw)] = {"complete": len(done) == len(matches), "played": len(done),
                        "total": len(matches), "fetched_at": _now_iso()}
        log(f"    L1 {season} J{gw} : {len(done)}/{len(matches)} matchs → {n_rows} lignes")
        _save(cache_name, data)
    data["n_gw"] = max((int(g) for g, v in gws.items() if v.get("played")), default=0)
    _save(cache_name, data)
    if fixtures:
        _save("l1_fixtures.json", fixtures)
    return data, (fixtures or _load("l1_fixtures.json"))


def load_ratings(fetch: bool = True, log=print) -> tuple[dict, dict | None]:
    """Notes L1 depuis le cache, rafraîchies via l'API MPG si possible."""
    cache_name = f"ratings_{L1_SEASON}.json"
    cached = _load(cache_name)
    fixtures = _load("l1_fixtures.json")
    if fetch:
        try:
            from mpg_client import build_client
            client, _, _ = build_client()
            return refresh_ratings(client, L1_SEASON, log=log)
        except Exception as exc:
            log(f"  ⚠ Notes L1 non rafraîchies ({exc}) — cache local utilisé")
    if not cached:
        raise RuntimeError(f"Aucune note L1 : {CACHE / cache_name} absent et refetch impossible")
    cached.setdefault("n_gw", max((int(g) for g, v in (cached.get("gws") or {}).items() if v.get("played")), default=0))
    return cached, fixtures


def club_next_match(fixtures: dict | None, stats_by: dict, idx: dict) -> dict[str, dict]:
    """{club_id: {home, opp, date}} pour la prochaine journée L1 — fixtures, sinon stats.nextMatch du pool."""
    out: dict[str, dict] = {}
    for m in (fixtures or {}).get("matches") or []:
        if m.get("home"):
            out[m["home"]] = {"home": True, "opp": m.get("away"), "date": m.get("date")}
        if m.get("away"):
            out[m["away"]] = {"home": False, "opp": m.get("home"), "date": m.get("date")}
    if out:
        return out
    for pid, st in stats_by.items():
        nm = st.get("nextMatch") or {}
        club = (idx.get(pid) or {}).get("clubId")
        if not nm or not club or club in out:
            continue
        side = nm.get("side")
        if side not in ("home", "away"):
            continue
        opp = ((nm.get("away") if side == "home" else nm.get("home")) or {}).get("clubId")
        out[club] = {"home": side == "home", "opp": opp, "date": nm.get("date")}
    return out


# ── Scoring (port de supabase/functions/best-team/index.ts) ──────────────────

def score_player(entries: list[dict], is_home: bool) -> dict:
    rated = [e for e in sorted(entries, key=lambda e: -int(e.get("gw") or 0)) if e.get("rating") is not None][:WINDOW]
    n = len(rated)
    if n == 0:
        return {"score": 0.0, "n": 0, "goal_rate": 0.0, "assist_rate": 0.0}
    ws = wr = wg = wa = 0.0
    for i, e in enumerate(rated):   # du plus récent au plus ancien
        w = DECAY ** i
        wr += float(e["rating"]) * w
        ws += w
        wg += (e.get("goals") or 0) * w
        wa += (e.get("assists") or 0) * w
    avg, goal_rate, assist_rate = wr / ws, wg / ws, wa / ws
    score = avg + goal_rate * 0.3 + assist_rate * 0.15 + (HOME_BONUS if is_home else 0.0)
    return {"score": score, "n": n, "goal_rate": goal_rate, "assist_rate": assist_rate}


def data_flag(n: int) -> str | None:
    if n < 3:
        return FLAG_VERY_LIMITED
    if n <= 5:
        return FLAG_LIMITED
    return None


def best_formation(players: list[dict], forced: str | None = None) -> tuple[str | None, list[dict], list[dict]]:
    by_pos: dict[str, list[dict]] = {"G": [], "D": [], "M": [], "A": []}
    for p in players:
        if p["pos"] in by_pos:
            by_pos[p["pos"]].append(p)
    for lst in by_pos.values():
        lst.sort(key=lambda p: -p["score"])
    to_test = {forced: FORMATIONS[forced]} if forced and forced in FORMATIONS else FORMATIONS
    best, best_score, best_starters = None, float("-inf"), []
    for name, slots in to_test.items():
        starters: list[dict] = []
        total = 0.0
        valid = True
        for pos, count in slots.items():
            if len(by_pos[pos]) < count:
                valid = False
                break
            picked = by_pos[pos][:count]
            starters += picked
            total += sum(p["score"] for p in picked)
        if not valid:
            continue
        d = slots.get("D", 0)          # bonus défense MPG : 5D → +1.0/def, 4D → +0.5/def
        if d >= 5:
            total += d * 1.0
        elif d == 4:
            total += d * 0.5
        if total > best_score:
            best, best_score, best_starters = name, total, starters
    ids = {p["id"] for p in best_starters}
    subs = sorted((p for p in players if p["id"] not in ids), key=lambda p: -p["score"])
    return best, best_starters, subs


def pick_captain(starters: list[dict]) -> tuple[dict | None, str]:
    if not starters:
        return None, ""
    cands = [p for p in starters if (p.get("avg") or 0) > 6] or list(starters)
    cands.sort(key=lambda p: (-p["score"], CAPTAIN_PREF.get(p["pos"], 9)))
    cap = cands[0]
    why = {"A": "attaquant, gros upside bonus", "M": "milieu offensif, bon upside"}.get(cap["pos"], "solide et régulier")
    return cap, f"Meilleur score ({cap['score']:.2f}) — {why}"


# ── Commentaire IA ───────────────────────────────────────────────────────────

def _ai_prompt(formation: str, starters: list[dict], captain: dict, opponent: str | None, gw) -> str:
    parts = [int(x) for x in formation.split("-")]
    detail = f"{formation} (1 gardien, {parts[0]} défenseurs, {parts[1]} milieux, {parts[2]} attaquants)"
    lst = ", ".join(
        f"{s['name']} ({POS_FR.get(s['pos'], s['pos'])}, score {s['score']:.2f}{', ' + s['flag'] if s.get('flag') else ''})"
        for s in starters
    )
    fragile = [s["name"] for s in starters if s.get("flag")]
    fragile_note = f"\nJoueurs à surveiller (données limitées ou forme incertaine) : {', '.join(fragile)}." if fragile else ""
    if opponent:
        opp_rule = f"l'adversaire (\"{opponent} va souffrir\", \"bon courage à eux\")"
        when = f"Journée {gw} contre {opponent}" if gw else f"Prochaine journée contre {opponent}"
    else:
        opp_rule = ("l'adversaire (pas encore connu : parle de « l'adversaire » ou « les autres » "
                    "sans inventer de nom ni laisser de crochets)")
        when = "Prochaine journée (adversaire pas encore connu)"
    return (
        "Tu es une IA qui vient de composer cette équipe MPG et qui assume complètement ses choix. "
        "Tu commentes la compo dans le groupe WhatsApp de la ligue — avec humour, vannes légères, en mode pote taquin. "
        "Tutoiement, 3-4 phrases fluides, pas de listes, pas de \"Yo\" systématique.\n\n"
        "Règles absolues :\n"
        "- Tu ASSUMES ta compo, tu ne la remets JAMAIS en cause\n"
        "- Tu ne suggères PAS de changer des joueurs (roster figé)\n"
        f"- Le chambrage porte sur : {opp_rule}, les joueurs fragiles "
        "ou en méforme de ta propre équipe (avec une vanne affectueuse), la situation tactique (ex: si un seul attaquant, "
        "il faut qu'il soit chaud), et le capitaine (justifier le choix avec une vanne)\n\n"
        f"{when}, formation {detail}.\n"
        f"Compo : {lst}\n"
        f"Capitaine : {captain['name']} ({POS_FR.get(captain['pos'], captain['pos'])}){fragile_note}"
    )


def _ai_commentary(prompt: str) -> str:
    from anthropic import Anthropic
    msg = Anthropic().messages.create(model=AI_MODEL, max_tokens=300,
                                      messages=[{"role": "user", "content": prompt}])
    return "".join(getattr(b, "text", "") for b in msg.content).strip()


def _fallback_commentary(formation: str, starters: list[dict], captain: dict, opponent: str | None) -> str:
    # on ne cite que les joueurs vraiment fragiles (< 3 matchs), sinon les 3-5 matchs s'ils sont minoritaires
    fragile = [s["last"] for s in starters if s.get("flag") == FLAG_VERY_LIMITED]
    if not fragile:
        limited = [s["last"] for s in starters if s.get("flag")]
        fragile = limited if len(limited) <= 4 else []
    n_att = FORMATIONS[formation]["A"]
    txt = (f"Compo en {formation}, {captain['name']} porte le brassard "
           f"({POS_FR.get(captain['pos'], '').lower()}, score {captain['score']:.2f}).")
    if n_att == 1:
        txt += f" Un seul attaquant devant, {starters[-1]['last']} a intérêt à être chaud."
    if fragile:
        txt += f" À surveiller faute de données : {', '.join(fragile[:4])}."
    txt += f" {opponent} va devoir sortir le grand jeu." if opponent else " Bon courage à l'adversaire."
    return txt


def build_commentary(person_id: str, division: str, formation: str, starters: list[dict], captain: dict,
                     next_match: dict | None, cache: dict, use_ai: bool = True, log=print) -> tuple[str, bool, bool]:
    """(texte, généré par l'IA ?, servi depuis le cache ?)"""
    opponent = (next_match or {}).get("opponent")
    gw = (next_match or {}).get("game_week")
    key = hashlib.sha1("|".join([PROMPT_VERSION, division, str(gw), opponent or "", formation,
                                 ",".join(sorted(s["id"] for s in starters)), captain["id"]]).encode()).hexdigest()[:16]
    ai_available = use_ai and bool(os.environ.get("ANTHROPIC_API_KEY"))
    cached = cache.get(person_id) or {}
    if cached.get("key") == key and cached.get("text") and (cached.get("ai") or not ai_available):
        return cached["text"], bool(cached.get("ai")), True
    text, ai = None, False
    if ai_available:
        try:
            text = _ai_commentary(_ai_prompt(formation, starters, captain, opponent, gw))
            ai = bool(text)
        except Exception as exc:
            log(f"  ⚠ Commentaire IA échoué pour {person_id} ({exc}) — gabarit local")
    if not text:
        text = _fallback_commentary(formation, starters, captain, opponent)
    cache[person_id] = {"key": key, "text": text, "ai": ai, "generated_at": _now_iso()}
    return text, ai, False


# ── Construction ─────────────────────────────────────────────────────────────

def _build_player(pid: str, info: dict, idx: dict, stats_by: dict, clubs: dict, db_names: dict,
                  ratings: dict, club_next: dict) -> dict:
    ix = idx.get(pid) or {}
    dbn = db_names.get(pid) or {}
    st = stats_by.get(pid) or {}
    entries = (ratings.get("players") or {}).get(pid) or []
    first = ix.get("firstName") or dbn.get("first") or ""
    last = ix.get("lastName") or dbn.get("last") or ""
    name = f"{first} {last}".strip() or pid.replace("mpg_championship_player_", "joueur ")
    pos = _pos_label(ix.get("position"), ix.get("ultraPosition")) if ix else (dbn.get("pos") or "M")
    latest = max(entries, key=lambda e: e.get("gw") or 0) if entries else None
    club_id = (latest or {}).get("club") or ix.get("clubId") or dbn.get("club_id")
    nxt = club_next.get(club_id) if club_id else None
    sc = score_player(entries, bool(nxt and nxt.get("home")))
    flag = data_flag(sc["n"])
    rated_all = [float(e["rating"]) for e in entries if e.get("rating") is not None]
    if rated_all:
        avg = round(sum(rated_all) / len(rated_all), 2)
    else:
        avg = st.get("averageRating")
    recent = sorted((e for e in entries if e.get("rating") is not None), key=lambda e: -(e.get("gw") or 0))[:5]
    return {
        "id": pid, "name": name, "last": last or name, "pos": pos,
        "club": _short_club(clubs, club_id) if club_id else "", "club_id": club_id,
        "cote": ix.get("quotation"), "price": info.get("price"),
        "score": 0.0 if flag == FLAG_VERY_LIMITED else round(sc["score"], 2),
        "raw_score": round(sc["score"], 2), "avg": avg, "n": sc["n"], "flag": flag,
        "goals": sum(e.get("goals") or 0 for e in entries),
        "assists": sum(e.get("assists") or 0 for e in entries),
        "form": [e["rating"] for e in recent],
        "next_home": (nxt or {}).get("home"),
        "next_opp": _short_club(clubs, nxt["opp"]) if nxt and nxt.get("opp") else None,
    }


def build_bestteam_data(conn=None, division: str | None = None, fetch: bool = True,
                        ai: bool = True, log=print) -> dict:
    """Données injectées dans bestteam.html (const BESTTEAM)."""
    own = conn is None
    if own:
        conn = get_conn()
    try:
        _load_env()
        div = pick_division(conn, division)
        teams = load_teams(conn, div)
        if not teams:
            raise RuntimeError(f"Aucune équipe en base pour {div}")
        display = _display_names()
        idx, stats_by = load_player_index()
        clubs = _load("clubs.json", {}) or {}
        db_names = names_from_db(conn, div)
        ratings, fixtures = load_ratings(fetch, log)
        club_next = club_next_match(fixtures, stats_by, idx)
        mpg_next, finished = next_mpg_matches(conn, div)
        cache = _load("bestteam_commentary.json", {}) or {}

        managers = []
        n_ai = n_cached = 0
        for t in sorted(teams, key=lambda t: display.get(t["person_id"], t["person_id"]).lower()):
            players = [_build_player(pid, info or {}, idx, stats_by, clubs, db_names, ratings, club_next)
                       for pid, info in t["squad"].items()]
            players.sort(key=lambda p: (POS_ORDER.get(p["pos"], 9), -p["score"], p["last"]))
            formation, starters, _subs = best_formation(players)
            nm = mpg_next.get(t["person_id"])
            captain, reason = pick_captain(starters)
            if formation and captain:
                text, is_ai, from_cache = build_commentary(t["person_id"], div, formation, starters, captain,
                                                           nm, cache, use_ai=ai, log=log)
                n_ai += is_ai
                n_cached += from_cache
            else:
                text, is_ai = "Pas assez de joueurs pour aligner une équipe complète.", False
            managers.append({
                "person_id": t["person_id"],
                "display": display.get(t["person_id"], t["person_id"]),
                "team_name": t["name"],
                "budget": t["budget"],
                "next_match": nm,
                "players": players,
                "formation": formation,
                "starters": [s["id"] for s in starters],
                "captain": captain["id"] if captain else None,
                "captain_reason": reason,
                "commentary": text,
                "commentary_ai": is_ai,
            })
        _save("bestteam_commentary.json", cache)
        log(f"    commentaires : {n_ai} IA, {len(managers) - n_ai} gabarit, {n_cached} depuis le cache")
        return {
            "generated_at": _now_iso(),
            "division_id": div,
            "label": _slabel(div),
            "season_finished": finished,
            "window": WINDOW,
            "l1": {"season": L1_SEASON, "n_gw": ratings.get("n_gw") or 0,
                   "next_gw": (fixtures or {}).get("gw"), "n_fixtures": len(club_next)},
            "managers": managers,
        }
    finally:
        if own:
            conn.close()


# ── CLI ──────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="Best Team statique (debug)")
    ap.add_argument("--division", help="division_id à traiter (défaut : courante)")
    ap.add_argument("--no-fetch", action="store_true", help="ne pas rafraîchir les notes L1 via l'API MPG")
    ap.add_argument("--no-ai", action="store_true", help="pas d'appel Anthropic (gabarit local)")
    ap.add_argument("--json", help="écrit le JSON complet dans ce fichier")
    args = ap.parse_args()

    data = build_bestteam_data(division=args.division, fetch=not args.no_fetch, ai=not args.no_ai)
    print(f"\n{data['label']} ({data['division_id']}) — notes L1 {data['l1']['season']} jusqu'à J{data['l1']['n_gw']}, "
          f"prochaine J{data['l1']['next_gw']} ({data['l1']['n_fixtures']} clubs avec affiche)")
    for m in data["managers"]:
        nm = m["next_match"] or {}
        head = (f"J{nm['game_week']} vs {nm['opponent']}" if nm
                else ("saison terminée" if data["season_finished"] else "prochaine journée ?"))
        print(f"\n=== {m['display']} — {m['team_name']} · {m['formation']} · {head}")
        by_id = {p["id"]: p for p in m["players"]}
        for sid in m["starters"]:
            p = by_id[sid]
            cap = " (C)" if sid == m["captain"] else ""
            flag = f"  {p['flag']}" if p["flag"] else ""
            print(f"  {p['pos']} {p['name'][:24]:24s} {p['club'][:6]:6s} {p['score']:5.2f} n={p['n']:<2}{cap}{flag}")
        print(f"  → {m['commentary']}")
    if args.json:
        Path(args.json).write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\nJSON écrit : {args.json}")


if __name__ == "__main__":
    main()
