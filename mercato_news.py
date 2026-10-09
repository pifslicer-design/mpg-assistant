"""mercato_news.py — Résumé humoristique d'un tour de mercato, publié dans les news du site.

Construit les faits du tour (achats, prix, surcotes, bonnes affaires, budgets restants) depuis
mpg.db (teams.raw_json.squad, bidMercatoTurn) et le cache local mercato_cache/ (identités des
joueurs, notes L1 2026), demande le texte à Claude (même ton que les résumés post-journée),
puis l'enregistre dans journee_recap avec game_week = 100 + tour. generate_pages.py (index)
l'affiche en tête de l'accueil s'il est plus récent que le dernier résumé de journée.

Usage (venv WSL) :
    python mercato_news.py                 # tour = dernier tour résolu de la division courante
    python mercato_news.py --turn 1 --force
    python mercato_news.py --dry-run       # affiche le résumé sans l'enregistrer
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
from collections import defaultdict
from pathlib import Path

BASE = Path(__file__).parent
CACHE = BASE / "mercato_cache"
DB_PATH = BASE / "mpg.db"
MERCATO_GW_BASE = 100          # game_week des entrées mercato dans journee_recap : 100 + tour
POS = {1: "G", 2: "D", 3: "M", 4: "A"}
BUDGET = 500

try:
    from dotenv import load_dotenv
    load_dotenv(BASE / ".env", override=False)
except ImportError:
    pass


# ── Données ──────────────────────────────────────────────────────────────────

def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def _load_cache(name: str, default):
    p = CACHE / name
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def _club_name(clubs: dict, cid) -> str:
    c = clubs.get(cid) or {}
    n = c.get("name")
    if isinstance(n, dict):
        n = n.get("fr-FR") or next(iter(n.values()), None)
    return n or c.get("shortName") or "?"


def _display_names() -> dict[str, str]:
    try:
        import yaml
        data = yaml.safe_load((BASE / "people_mapping.yaml").read_text(encoding="utf-8")) or {}
        people = data.get("persons") or data.get("people") or data
        out = {}
        for pid, info in people.items():
            if isinstance(info, dict):
                out[pid] = info.get("display_name") or info.get("display") or info.get("name") or pid.capitalize()
            else:
                out[pid] = pid.capitalize()
        return out
    except Exception:
        return {}


def current_division(conn) -> str:
    try:
        from mpg_db import get_current_division
        return get_current_division()[0]
    except Exception:
        row = conn.execute("SELECT division_id FROM divisions_metadata WHERE is_current=1 LIMIT 1").fetchone()
        return row["division_id"] if row else ""


def season_of(conn, division_id: str) -> int:
    row = conn.execute("SELECT season FROM divisions_metadata WHERE division_id=?", (division_id,)).fetchone()
    if row and row["season"]:
        return int(row["season"])
    r26 = _load_cache("ratings_2026.json", {})
    return int(r26.get("season") or 2026)


def slabel(division_id: str) -> str:
    try:
        from generate_pages import slabel as _sl
        return _sl(division_id)
    except Exception:
        m = re.search(r"_(\d+)_\d+$", division_id)
        return f"S{m.group(1)}" if m else division_id


def fetch_division_history(division_id: str, fetch: bool = True) -> dict | None:
    """Coulisses du mercato : /division-history/division/{id} → mercato[tour][joueur] = {wonBid, lostBids}.
    Mis en cache dans mercato_cache/division_history.json (repli sur le cache si l'API échoue)."""
    cache_name = "division_history.json"
    if fetch:
        try:
            from mpg_client import build_client
            client, _, _ = build_client()
            r = client.get(f"/division-history/division/{division_id}")
            if r.status_code == 200:
                data = r.json()
                if data.get("id", "").endswith(division_id.replace("mpg_division_", "")):
                    CACHE.mkdir(exist_ok=True)
                    (CACHE / cache_name).write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
                    return data
            print(f"[WARN] division-history → {r.status_code}, repli sur le cache")
        except Exception as exc:
            print(f"[WARN] division-history indisponible ({exc}), repli sur le cache")
    data = _load_cache(cache_name, None)
    if data and data.get("id", "").endswith(division_id.replace("mpg_division_", "")):
        return data
    return None


def build_backstage(history: dict | None, turn: int, team2who: dict, names: dict, index: dict) -> dict | None:
    """Enchères gagnées ET perdues du tour (coulisses) : loupés de peu, surpayes, duels, joueurs disputés."""
    if not history:
        return None
    players = ((history.get("mercato") or {}).get(str(turn))) or {}
    if not players:
        return None
    per = defaultdict(lambda: {"won": [], "lost": []})
    contested, overpays, solo, ties = [], [], [], []
    duels = defaultdict(int)
    for pid, p in players.items():
        name = p.get("lastName") or (index.get(pid) or {}).get("lastName") or pid
        pos = POS.get(p.get("position"), "?")
        cote = p.get("quotation") or 0
        won = p.get("wonBid") or {}
        lost = p.get("lostBids") or []
        w = team2who.get(won.get("teamId"), won.get("teamId"))
        wn = names.get(w, str(w).capitalize())
        best_lost = max((b.get("price") or 0 for b in lost), default=None)
        per[w]["won"].append({"name": name, "pos": pos, "cote": cote, "price": won.get("price"), "second_bid": best_lost, "n_bids": 1 + len(lost)})
        for b in lost:
            l = team2who.get(b.get("teamId"), b.get("teamId"))
            margin = (won.get("price") or 0) - (b.get("price") or 0)
            per[l]["lost"].append({"name": name, "pos": pos, "cote": cote, "bid": b.get("price"), "winner": wn,
                                   "win_price": won.get("price"), "margin": margin})
            duels[(names.get(l, str(l).capitalize()), wn)] += 1
            if margin == 0:
                ties.append({"name": name, "winner": wn, "loser": names.get(l, str(l).capitalize()), "price": won.get("price")})
        if lost:
            contested.append({"name": name, "pos": pos, "cote": cote, "n_bids": 1 + len(lost), "winner": wn, "price": won.get("price"),
                              "losers": sorted([{"manager": names.get(team2who.get(b.get("teamId")), "?"), "bid": b.get("price")} for b in lost],
                                               key=lambda x: -(x["bid"] or 0))})
            overpays.append({"manager": wn, "name": name, "pos": pos, "price": won.get("price"), "second_bid": best_lost,
                             "gap_vs_second": (won.get("price") or 0) - best_lost})
        else:
            solo.append({"manager": wn, "name": name, "pos": pos, "cote": cote, "price": won.get("price"),
                         "gap_vs_cote": (won.get("price") or 0) - cote})
    managers = []
    for who, d in per.items():
        won, lost = d["won"], d["lost"]
        total = len(won) + len(lost)
        near = sorted([l for l in lost if l["margin"] <= 3], key=lambda l: l["margin"])
        managers.append({
            "manager": names.get(who, str(who).capitalize()), "bids": total, "won": len(won), "lost": len(lost),
            "success_rate": round(len(won) / total, 2) if total else None,
            "near_misses": near[:4],
            "biggest_lost_bids": sorted(lost, key=lambda l: -(l["bid"] or 0))[:3],
            "lost_bids_total_amount": sum(l["bid"] or 0 for l in lost),
        })
    managers.sort(key=lambda m: -m["lost"])
    return {
        "note": "Enchères gagnées et perdues de tous les managers (coulisses du mercato, visibles par tous dans l'appli). "
                "margin = prix du gagnant - enchère perdue ; margin 0 = égalité départagée par l'appli.",
        "managers": managers,
        "most_contested": sorted(contested, key=lambda c: -c["n_bids"])[:8],
        "overpays_vs_second_bid": sorted(overpays, key=lambda o: -o["gap_vs_second"])[:8],
        "uncontested_expensive": sorted(solo, key=lambda s: -(s["price"] or 0))[:6],
        "ties": ties,
        "duels": [{"loser": l, "winner": w, "times": n} for (l, w), n in sorted(duels.items(), key=lambda x: -x[1])[:8]],
        "total_bids_on_bought_players": sum(1 + len(p.get("lostBids") or []) for p in players.values()),
    }


def build_facts(conn, division_id: str, turn: int | None = None, fetch: bool = True) -> dict | None:
    index = _load_cache("index.json", {})
    clubs = _load_cache("clubs.json", {})
    pool = _load_cache("pool.json", {})
    r26 = _load_cache("ratings_2026.json", {"players": {}})
    ratings = r26.get("players") or {}
    names = _display_names()

    teams = conn.execute(
        "SELECT id, name, person_id, budget, raw_json FROM teams WHERE division_id=?", (division_id,)
    ).fetchall()
    if not teams:
        return None

    def perf(pid):
        g = [x for x in ratings.get(pid, []) if x.get("rating") is not None]
        if not g:
            return None
        return {"n": len(g), "note": round(sum(x["rating"] for x in g) / len(g), 2),
                "buts": sum(x.get("goals") or 0 for x in g), "tit": sum(1 for x in g if not x.get("sub"))}

    rows = []
    for t in teams:
        who = t["person_id"] or t["name"]
        squad = (json.loads(t["raw_json"] or "{}") or {}).get("squad") or {}
        for pid, info in squad.items():
            ix = index.get(pid) or {}
            rows.append({
                "who": who, "manager": names.get(who, who.capitalize()), "team": t["name"], "pid": pid,
                "name": ix.get("lastName") or pid.replace("mpg_championship_player_", "joueur_"),
                "pos": POS.get(ix.get("position"), "?"), "club": _club_name(clubs, ix.get("clubId")),
                "cote": ix.get("quotation") or 0, "price": info.get("price") or 0,
                "turn": info.get("bidMercatoTurn"), "perf": perf(pid),
            })
    turns = sorted({r["turn"] for r in rows if r["turn"]})
    if not turns:
        return None
    turn = turn or turns[-1]
    t_rows = [r for r in rows if r["turn"] == turn]
    if not t_rows:
        return None

    budgets = {(t["person_id"] or t["name"]): t["budget"] for t in teams}
    by = defaultdict(list)
    for r in t_rows:
        by[r["who"]].append(r)
    managers = []
    for who, lst in sorted(by.items(), key=lambda x: -sum(r["price"] for r in x[1])):
        cnt = defaultdict(int)
        for r in lst:
            cnt[r["pos"]] += 1
        squad_total = len([r for r in rows if r["who"] == who])
        managers.append({
            "manager": names.get(who, who.capitalize()), "team": lst[0]["team"], "n": len(lst),
            "spent": sum(r["price"] for r in lst), "budget_left": budgets.get(who),
            "squad_total": squad_total, "by_pos": {p: cnt[p] for p in ("G", "D", "M", "A")},
            "top": [{"name": r["name"], "pos": r["pos"], "club": r["club"], "cote": r["cote"], "price": r["price"],
                     "perf": r["perf"]} for r in sorted(lst, key=lambda r: -r["price"])[:3]],
        })
    silent = [names.get(t["person_id"], t["person_id"]) for t in teams if (t["person_id"] or t["name"]) not in by]

    def brief(r):
        p = r["perf"] or {}
        return {"manager": r["manager"], "name": r["name"], "pos": r["pos"], "club": r["club"], "cote": r["cote"],
                "price": r["price"], "ratio": round(r["price"] / max(r["cote"], 1), 1),
                "note_2026": p.get("note"), "buts_2026": p.get("buts"), "titulaire": f"{p.get('tit')}/{p.get('n')}" if p else None}

    top_prices = [brief(r) for r in sorted(t_rows, key=lambda r: -r["price"])[:12]]
    surcotes = [brief(r) for r in sorted([r for r in t_rows if r["cote"] >= 8], key=lambda r: -(r["price"] / r["cote"]))[:6]]
    bargains = [brief(r) for r in sorted([r for r in t_rows if r["perf"] and r["perf"]["note"] >= 6.0 and r["price"] <= r["cote"] + 2],
                                         key=lambda r: -r["perf"]["note"])[:6]]
    dubious = [brief(r) for r in sorted([r for r in t_rows if r["price"] >= 20 and r["perf"] and
                                         (r["perf"]["note"] < 5.0 or r["perf"]["tit"] * 2 < r["perf"]["n"])], key=lambda r: -r["price"])[:6]]
    no_data = [brief(r) for r in t_rows if r["price"] >= 20 and not r["perf"]]

    # Record historique (toutes divisions, achats au T1 et tous tours confondus)
    hist_t1, hist_all = 0, 0
    for t in conn.execute("SELECT raw_json FROM teams WHERE division_id != ?", (division_id,)):
        for info in ((json.loads(t["raw_json"] or "{}") or {}).get("squad") or {}).values():
            p = info.get("price") or 0
            hist_all = max(hist_all, p)
            if info.get("bidMercatoTurn") == 1:
                hist_t1 = max(hist_t1, p)
    top = max(t_rows, key=lambda r: r["price"])
    by_pos_spend = defaultdict(int)
    for r in t_rows:
        by_pos_spend[r["pos"]] += r["price"]
    tot = sum(by_pos_spend.values()) or 1
    clubs_cnt = defaultdict(int)
    for r in t_rows:
        clubs_cnt[r["club"]] += 1

    available = []
    for p in (pool.get("availablePlayers") or []):
        pf = perf(p["id"]) or {}
        available.append({"name": p.get("lastName"), "pos": POS.get(p.get("position"), "?"), "club": _club_name(clubs, p.get("clubId")),
                          "cote": p.get("quotation") or 0, "note_2026": pf.get("note"), "buts_2026": pf.get("buts")})
    available.sort(key=lambda p: -(p["cote"] or 0))

    team2who = {t["id"]: (t["person_id"] or t["name"]) for t in teams}
    backstage = build_backstage(fetch_division_history(division_id, fetch=fetch), turn, team2who, names, index)

    return {
        "backstage": backstage,
        "division_id": division_id, "slabel": slabel(division_id), "turn": turn, "n_turns_done": len(turns),
        "n_purchases": len(t_rows), "total_spent": sum(r["price"] for r in t_rows), "budget_pool": BUDGET * len(teams),
        "managers": managers, "silent_managers": silent,
        "top_prices": top_prices, "surcotes": surcotes, "bargains": bargains, "dubious": dubious, "no_data_buys": no_data,
        "record": {"this_turn_max": top["price"], "player": top["name"], "manager": top["manager"],
                   "historic_t1_max": hist_t1, "historic_all_turns_max": hist_all,
                   "is_t1_record": turn == 1 and top["price"] > hist_t1, "is_all_time_record": top["price"] > hist_all},
        "n_buys_50plus": len([r for r in t_rows if r["price"] >= 50]),
        "spend_share_by_pos": {p: round(v / tot, 2) for p, v in by_pos_spend.items()},
        "most_bought_clubs": sorted(clubs_cnt.items(), key=lambda x: -x[1])[:5],
        "still_available_top": available[:8],
        "min_squad": "2 G / 6 D / 6 M / 4 A (18 joueurs)",
    }


# ── Rédaction ────────────────────────────────────────────────────────────────

MERCATO_PROMPT = """Tu rédiges maintenant le RÉSUMÉ D'UN TOUR DE MERCATO (enchères aveugles, budget 500 par manager,
l'argent des enchères perdues revient au tour suivant, effectif minimum 2 G / 6 D / 6 M / 4 A). Même ton que
les résumés de journée : potes, sarcastique, charrieur, sans méchanceté gratuite, spécifique, pas de clichés.

Tu reçois les faits du tour en JSON (achats par manager, prix payés, cote de départ, surcotes, bonnes affaires,
achats douteux au vu des notes de la saison en cours, record historique, budgets restants, stars encore libres).
Ne cite que des faits présents dans le JSON. Les prix sont en millions, la "cote" est le prix de départ.

Le JSON contient aussi "backstage" : les coulisses du mercato, c'est-à-dire TOUTES les enchères, gagnées et
perdues, de tous les managers (l'appli les montre à tout le monde une fois le tour résolu). C'est la partie la
plus drôle : les loupés de peu (margin 1 à 3 : « il a raté X pour 2M »), les égalités perdues (margin 0), ceux
qui ont perdu beaucoup d'enchères (lost, success_rate), les surpayes par rapport à la deuxième enchère
(overpays_vs_second_bid : « il a payé 76 alors que la deuxième offre était à 24 »), les achats sans aucune
concurrence payés cher (uncontested_expensive), les joueurs les plus disputés (most_contested) et les duels
(duels : qui a battu qui le plus souvent). Félicite ceux qui ont un bon taux de réussite, charrie les autres.

Réponds STRICTEMENT en JSON brut :
{
  "title": "Titre punchy de 6-14 mots sur LE fait marquant du tour (record, folie d'un manager, panique…)",
  "summary_md": "10 à 14 puces markdown, chacune avec un emoji, en français, max 32 mots par puce"
}

Contenu attendu des puces, dans cet ordre de priorité :
1. Le fait marquant (record de prix, achat le plus fou) avec le ratio prix/cote et la deuxième enchère.
2. Un mot sur CHAQUE manager (8 au total, en gras **Nom**), avec son achat le plus cher ET le prix payé
   (champ top du JSON), son taux de réussite aux enchères (backstage.managers : won/bids) et son loupé le plus
   rageant ou sa plus belle victoire ; une puce par manager.
3. Les surpayes vs deuxième enchère et les achats sans concurrence payés cher.
4. Les joueurs les plus disputés et les duels récurrents.
5. Les bonnes affaires (joueurs bien notés payés à la cote) et les achats douteux (mauvaise note ou remplaçant).
6. Le marché : budgets restants, une ou deux stars encore libres pour le tour suivant.

Exactitude obligatoire :
- Toute comparaison (« la plus grosse caisse », « le plus dépensier », « le plus cher », « le plus de loupés »)
  doit être vérifiée dans le JSON. Pas d'approximation, pas d'arrondi inventé.
- Les effectifs par poste viennent de by_pos (G/D/M/A) : n'invente pas de détail de poste (central, latéral…)
  ni d'information absente du JSON.
- Chaque enchère perdue citée doit venir de backstage (nom, montant misé, gagnant, prix du gagnant)."""


def write_mercato_summary(facts: dict) -> dict:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if api_key:
        try:
            import anthropic
            from summary_writer import SYSTEM_PROMPT, CLAUDE_MODEL
            client = anthropic.Anthropic(api_key=api_key)
            msg = client.messages.create(
                model=CLAUDE_MODEL, max_tokens=2000, temperature=0.8,
                system=SYSTEM_PROMPT + "\n\n" + MERCATO_PROMPT,
                messages=[{"role": "user", "content": json.dumps(facts, ensure_ascii=False, default=str)}],
            )
            text = "".join(getattr(b, "text", "") for b in msg.content).strip()
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S)
            data = json.loads(text)
            if data.get("title") and data.get("summary_md"):
                return {"title": str(data["title"]), "summary_md": str(data["summary_md"])}
        except Exception as exc:  # pragma: no cover
            print(f"[WARN] Claude indisponible ({exc}) — gabarit local")
    return _fallback(facts)


def _fallback(f: dict) -> str | dict:
    rec = f["record"]
    lines = [f"- 💸 **{rec['manager']}** signe l'achat le plus cher du tour : {rec['player']} à {rec['this_turn_max']}"
             + (" — record historique de la ligue." if rec["is_all_time_record"] else ".")]
    for m in f["managers"]:
        top = m["top"][0] if m["top"] else None
        lines.append(f"- 🛒 **{m['manager']}** : {m['n']} joueurs pour {m['spent']}, reste {m['budget_left']}"
                     + (f", gros coup {top['name']} à {top['price']}." if top else "."))
    for s in f["silent_managers"]:
        lines.append(f"- 😴 **{s}** n'a rien acheté ce tour.")
    return {"title": f"Mercato {f['slabel']} tour {f['turn']} : {rec['player']} à {rec['this_turn_max']}",
            "summary_md": "\n".join(lines[:12])}



# ── Bilan de tout le mercato ─────────────────────────────────────────────────

BILAN_GW = MERCATO_GW_BASE + 99   # journee_recap.game_week du bilan (étiquette « Bilan mercato »)


def build_bilan_facts(conn, division_id: str, fetch: bool = True) -> dict | None:
    """Faits de tout le mercato : film tour par tour, bilan par manager, records, coulisses cumulées."""
    index = _load_cache("index.json", {})
    clubs = _load_cache("clubs.json", {})
    r26 = _load_cache("ratings_2026.json", {"players": {}})
    ratings = r26.get("players") or {}
    names = _display_names()
    teams = conn.execute(
        "SELECT id, name, person_id, budget, raw_json FROM teams WHERE division_id=?", (division_id,)
    ).fetchall()
    if not teams:
        return None
    team2who = {t["id"]: (t["person_id"] or t["name"]) for t in teams}

    def perf(pid):
        g = [x for x in ratings.get(pid, []) if x.get("rating") is not None]
        if not g:
            return None
        return {"n": len(g), "note": round(sum(x["rating"] for x in g) / len(g), 2),
                "buts": sum(x.get("goals") or 0 for x in g), "tit": sum(1 for x in g if not x.get("sub"))}

    rows = []
    for t in teams:
        who = t["person_id"] or t["name"]
        squad = (json.loads(t["raw_json"] or "{}") or {}).get("squad") or {}
        for pid, info in squad.items():
            ix = index.get(pid) or {}
            rows.append({"who": who, "manager": names.get(who, who.capitalize()), "team": t["name"], "pid": pid,
                         "name": ix.get("lastName") or pid, "pos": POS.get(ix.get("position"), "?"),
                         "club": _club_name(clubs, ix.get("clubId")), "cote": ix.get("quotation") or 0,
                         "price": info.get("price") or 0, "turn": info.get("bidMercatoTurn") or 0, "perf": perf(pid)})
    turns = sorted({r["turn"] for r in rows if r["turn"]})
    history = fetch_division_history(division_id, fetch=fetch)

    def brief(r):
        pf = r["perf"] or {}
        return {"manager": r["manager"], "name": r["name"], "pos": r["pos"], "club": r["club"], "cote": r["cote"],
                "price": r["price"], "turn": r["turn"], "ratio": round(r["price"] / max(r["cote"], 1), 1),
                "note_2026": pf.get("note"), "buts_2026": pf.get("buts"),
                "titulaire": f"{pf.get('tit')}/{pf.get('n')}" if pf else None}

    # Film tour par tour
    film = []
    for t in turns:
        tr = [r for r in rows if r["turn"] == t]
        bs = build_backstage(history, t, team2who, names, index) or {}
        film.append({
            "turn": t, "n_purchases": len(tr), "total_spent": sum(r["price"] for r in tr),
            "max_price": max(r["price"] for r in tr),
            "top": [brief(r) for r in sorted(tr, key=lambda r: -r["price"])[:5]],
            "buyers": {names.get(w, str(w).capitalize()): len([r for r in tr if r["who"] == w]) for w in {r["who"] for r in tr}},
            "n_bids": bs.get("total_bids_on_bought_players"),
            "most_contested": (bs.get("most_contested") or [])[:3],
            "overpays_vs_second_bid": (bs.get("overpays_vs_second_bid") or [])[:3],
            "ties": bs.get("ties") or [],
        })

    # Coulisses cumulées (toutes les enchères de tous les tours)
    cum = defaultdict(lambda: {"won": 0, "lost": 0, "near": [], "lost_big": [], "lost_amount": 0})
    duels = defaultdict(int)
    for t in turns:
        bs = build_backstage(history, t, team2who, names, index) or {}
        for m in bs.get("managers") or []:
            c = cum[m["manager"]]
            c["won"] += m["won"]; c["lost"] += m["lost"]; c["lost_amount"] += m["lost_bids_total_amount"]
            c["near"] += [dict(x, turn=t) for x in m["near_misses"]]
            c["lost_big"] += [dict(x, turn=t) for x in m["biggest_lost_bids"]]
        for d in bs.get("duels") or []:
            duels[(d["loser"], d["winner"])] += d["times"]

    # Bilan par manager
    budgets = {(t["person_id"] or t["name"]): t["budget"] for t in teams}
    managers = []
    for who in sorted({r["who"] for r in rows}):
        lst = [r for r in rows if r["who"] == who]
        mname = names.get(who, who.capitalize())
        cnt = defaultdict(int)
        for r in lst:
            cnt[r["pos"]] += 1
        rated = [r for r in lst if r["perf"] and r["perf"]["n"] >= 3]
        starters = sorted(rated, key=lambda r: -r["perf"]["note"])[:11]
        c = cum.get(mname, {"won": 0, "lost": 0, "near": [], "lost_big": [], "lost_amount": 0})
        total_bids = c["won"] + c["lost"]
        managers.append({
            "manager": mname, "team": lst[0]["team"], "n_players": len(lst), "budget_left": budgets.get(who),
            "by_pos": {p: cnt[p] for p in ("G", "D", "M", "A")},
            "spent_by_turn": {f"T{t}": sum(r["price"] for r in lst if r["turn"] == t) for t in turns},
            "top_buys": [brief(r) for r in sorted(lst, key=lambda r: -r["price"])[:4]],
            "best_rated": [brief(r) for r in sorted(rated, key=lambda r: -r["perf"]["note"])[:3]],
            "squad_avg_note_top11": round(sum(r["perf"]["note"] for r in starters) / len(starters), 2) if starters else None,
            "squad_goals_2026": sum(r["perf"]["buts"] for r in lst if r["perf"]),
            "bids_total": total_bids, "bids_won": c["won"], "bids_lost": c["lost"],
            "success_rate": round(c["won"] / total_bids, 2) if total_bids else None,
            "near_misses": sorted(c["near"], key=lambda x: x["margin"])[:4],
            "biggest_lost_bids": sorted(c["lost_big"], key=lambda x: -(x["bid"] or 0))[:3],
            "lost_bids_total_amount": c["lost_amount"],
            "bargains": [brief(r) for r in lst if r["perf"] and r["perf"]["note"] >= 6.0 and r["price"] <= r["cote"] + 2][:4],
            "dubious": [brief(r) for r in lst if r["price"] >= 20 and r["perf"] and (r["perf"]["note"] < 5.0 or r["perf"]["tit"] * 2 < r["perf"]["n"])][:3],
        })
    managers.sort(key=lambda m: -(m["squad_avg_note_top11"] or 0))

    # Records et marché
    hist_t1 = hist_all = 0
    for t in conn.execute("SELECT raw_json FROM teams WHERE division_id != ?", (division_id,)):
        for info in ((json.loads(t["raw_json"] or "{}") or {}).get("squad") or {}).values():
            pr = info.get("price") or 0
            hist_all = max(hist_all, pr)
            if info.get("bidMercatoTurn") == 1:
                hist_t1 = max(hist_t1, pr)
    top_all = sorted(rows, key=lambda r: -r["price"])[:10]
    by_pos_spend = defaultdict(int)
    for r in rows:
        by_pos_spend[r["pos"]] += r["price"]
    tot = sum(by_pos_spend.values()) or 1
    clubs_cnt = defaultdict(int)
    for r in rows:
        clubs_cnt[r["club"]] += 1
    all_bargains = sorted([r for r in rows if r["perf"] and r["perf"]["n"] >= 3 and r["perf"]["note"] >= 6.0 and r["price"] <= r["cote"] + 3],
                          key=lambda r: -r["perf"]["note"])[:8]
    all_dubious = sorted([r for r in rows if r["price"] >= 25 and r["perf"] and (r["perf"]["note"] < 5.0 or r["perf"]["tit"] * 2 < r["perf"]["n"])],
                         key=lambda r: -r["price"])[:8]
    return {
        "division_id": division_id, "slabel": slabel(division_id), "n_turns": len(turns),
        "n_purchases": len(rows), "total_spent": sum(r["price"] for r in rows), "budget_pool": BUDGET * len(teams),
        "film": film, "managers": managers,
        "records": {"max_price": top_all[0]["price"], "player": top_all[0]["name"], "manager": top_all[0]["manager"], "turn": top_all[0]["turn"],
                    "historic_t1_max": hist_t1, "historic_all_turns_max": hist_all, "is_all_time_record": top_all[0]["price"] > hist_all},
        "top10_prices": [brief(r) for r in top_all],
        "spend_share_by_pos": {p: round(v / tot, 2) for p, v in by_pos_spend.items()},
        "most_bought_clubs": sorted(clubs_cnt.items(), key=lambda x: -x[1])[:6],
        "bargains": [brief(r) for r in all_bargains], "dubious": [brief(r) for r in all_dubious],
        "duels": [{"loser": l, "winner": w, "times": n} for (l, w), n in sorted(duels.items(), key=lambda x: -x[1])[:8]],
        "min_squad": "2 G / 6 D / 6 M / 4 A (18 joueurs)",
    }


BILAN_PROMPT = """Tu rédiges maintenant le BILAN COMPLET D'UN MERCATO (enchères aveugles par tours, budget 500 par manager,
l'argent des enchères perdues revient au tour suivant, effectif minimum 2 G / 6 D / 6 M / 4 A). Même ton que
les résumés de journée : potes, sarcastique, charrieur, sans méchanceté gratuite, spécifique, pas de clichés.

Tu reçois les faits du mercato en JSON : "film" (chaque tour : achats, dépense, top prix, joueurs disputés,
surpayes vs deuxième enchère, égalités), "managers" (par manager : effectif final par poste, dépense par tour,
plus gros achats, meilleurs joueurs à la note, moyenne de note des 11 meilleurs, taux de réussite aux enchères,
loupés de peu, grosses enchères perdues, bonnes affaires, achats douteux), "records", "top10_prices", "bargains",
"dubious", "duels", clubs les plus pillés, part de la dépense par poste. Ne cite que des faits du JSON.
Les prix sont en millions, la "cote" est le prix de départ. Les coulisses (enchères perdues) sont publiques.

Réponds STRICTEMENT en JSON brut :
{
  "title": "Titre punchy de 6-14 mots sur LE fait marquant du mercato",
  "summary_md": "markdown : 4 courts paragraphes-titres en gras (**Le film**, **Manager par manager**, **Les chiffres**, **Verdict**) suivis chacun de puces ; 16 à 20 puces en tout, chacune avec un emoji, max 32 mots par puce"
}

Contenu attendu :
- **Le film** (4 puces) : un mot par tour T1 → T5 (dépense, fait marquant, record, le tour 5 à un seul achat si c'est le cas).
- **Manager par manager** (8 puces, une par manager, en gras **Nom**) : effectif final (G/D/M/A), ses deux plus gros
  achats avec le prix, son taux de réussite aux enchères, son loupé le plus rageant OU sa meilleure affaire, et la
  qualité de l'effectif (squad_avg_note_top11 : compare les managers entre eux).
- **Les chiffres** (4 puces) : records (prix max, deuxième enchère), surpayes vs deuxième enchère, bonnes affaires,
  achats douteux (mauvaise note ou remplaçant), duels récurrents, clubs pillés.
- **Verdict** (2 puces) : qui a l'effectif le plus solide sur le papier (squad_avg_note_top11 + buts), qui part de loin.

Exactitude obligatoire : toute comparaison (« le meilleur taux », « la plus grosse dépense », « l'effectif le plus
fort ») doit être vérifiée dans le JSON ; les effectifs viennent de by_pos ; n'invente aucun détail absent."""


def write_bilan_summary(facts: dict) -> dict:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if api_key:
        try:
            import anthropic
            from summary_writer import SYSTEM_PROMPT, CLAUDE_MODEL
            client = anthropic.Anthropic(api_key=api_key)
            msg = client.messages.create(
                model=CLAUDE_MODEL, max_tokens=3500, temperature=0.8,
                system=SYSTEM_PROMPT + "\n\n" + BILAN_PROMPT,
                messages=[{"role": "user", "content": json.dumps(facts, ensure_ascii=False, default=str)}],
            )
            text = "".join(getattr(b, "text", "") for b in msg.content).strip()
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S)
            data = json.loads(text)
            if data.get("title") and data.get("summary_md"):
                return {"title": str(data["title"]), "summary_md": str(data["summary_md"])}
        except Exception as exc:  # pragma: no cover
            print(f"[WARN] Claude indisponible ({exc}) — gabarit local")
    rec = facts["records"]
    lines = [f"- 💸 Achat le plus cher du mercato : {rec['player']} à {rec['max_price']} par **{rec['manager']}** (T{rec['turn']})."]
    for m in facts["managers"]:
        bp = m["by_pos"]
        lines.append(f"- 🛒 **{m['manager']}** : {m['n_players']} joueurs (G{bp['G']} D{bp['D']} M{bp['M']} A{bp['A']}), "
                     f"réussite {m['success_rate']:.0%}, moyenne des 11 meilleurs {m['squad_avg_note_top11']}.")
    return {"title": f"Bilan du mercato {facts['slabel']}", "summary_md": "\n".join(lines)}


# ── Enregistrement ───────────────────────────────────────────────────────────

def save_news(conn, season: int, division_id: str, turn: int, facts: dict, summary: dict) -> None:
    from generate_pages import _ensure_recap_table, _save_recap
    _ensure_recap_table(conn)
    _save_recap(conn, season, division_id, MERCATO_GW_BASE + turn, [{"type": "mercato_turn", **facts}], summary)


def main() -> None:
    ap = argparse.ArgumentParser(description="Résumé humoristique d'un tour de mercato → news du site")
    ap.add_argument("--turn", type=int, help="tour à résumer (défaut : dernier tour résolu)")
    ap.add_argument("--division", help="division (défaut : courante)")
    ap.add_argument("--force", action="store_true", help="écrase un résumé déjà enregistré")
    ap.add_argument("--dry-run", action="store_true", help="affiche sans enregistrer")
    ap.add_argument("--no-ai", action="store_true", help="gabarit local, sans Claude")
    ap.add_argument("--bilan", action="store_true", help="bilan de tout le mercato (tous les tours)")
    ap.add_argument("--facts", help="écrit les faits JSON dans ce fichier (pour relecture)")
    args = ap.parse_args()

    conn = _conn()
    div = args.division or current_division(conn)
    if args.bilan:
        facts = build_bilan_facts(conn, div)
        if not facts:
            print("Aucun achat de mercato trouvé pour", div)
            return
        if args.facts:
            Path(args.facts).write_text(json.dumps(facts, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
            print("faits écrits dans", args.facts)
        season = season_of(conn, div)
        print(f"Bilan mercato {facts['slabel']} : {facts['n_turns']} tours, {facts['n_purchases']} achats, "
              f"{facts['total_spent']} dépensés, record {facts['records']['max_price']} ({facts['records']['player']})")
        if args.no_ai:
            os.environ.pop("ANTHROPIC_API_KEY", None)
        summary = write_bilan_summary(facts)
        print("\nTITRE :", summary["title"])
        print(summary["summary_md"])
        if args.dry_run:
            return
        from generate_pages import _ensure_recap_table, _save_recap
        _ensure_recap_table(conn)
        _save_recap(conn, season, div, BILAN_GW, [{"type": "mercato_bilan", **facts}], summary)
        print(f"\n✅ Bilan enregistré (season {season}, {div}, game_week {BILAN_GW}). Lance `python3 generate_pages.py index`.")
        return
    facts = build_facts(conn, div, args.turn)
    if not facts:
        print("Aucun achat de mercato trouvé pour", div)
        return
    season = season_of(conn, div)
    turn = facts["turn"]
    from generate_pages import _ensure_recap_table, _load_recap
    _ensure_recap_table(conn)
    if not args.force and not args.dry_run and _load_recap(conn, season, div, MERCATO_GW_BASE + turn):
        print(f"Résumé du tour {turn} déjà enregistré (utilise --force pour le refaire).")
        return
    print(f"Tour {turn} {facts['slabel']} : {facts['n_purchases']} achats, {facts['total_spent']} dépensés, "
          f"record {facts['record']['this_turn_max']} ({facts['record']['player']})")
    if args.no_ai:
        os.environ.pop("ANTHROPIC_API_KEY", None)
    summary = write_mercato_summary(facts)
    print("\nTITRE :", summary["title"])
    print(summary["summary_md"])
    if args.dry_run:
        return
    save_news(conn, season, div, turn, facts, summary)
    print(f"\n✅ Enregistré (season {season}, {div}, game_week {MERCATO_GW_BASE + turn}). "
          f"Lance `python3 generate_pages.py index` pour publier dans les news.")


if __name__ == "__main__":
    main()
