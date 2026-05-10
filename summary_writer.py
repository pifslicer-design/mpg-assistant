"""summary_writer.py — Transforme les événements détectés en résumé sarcastique.

Mode hybride :
  - LLM (Claude Sonnet 4.6) si ANTHROPIC_API_KEY présente
  - Fallback templates statiques sinon

Sortie : {"title": str, "summary_md": str}
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Optional

# Charge .env si dispo (sync_and_publish.sh ne le fait pas par défaut)
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env", override=False)
except ImportError:
    pass


CLAUDE_MODEL = "claude-sonnet-4-6"

SYSTEM_PROMPT = """Tu es le rédacteur du résumé post-journée d'une ligue MPG entre 8 potes \
qui se charrient depuis 2016. Ton style : vraiment potes, sarcastique, charrieur \
sans pitié mais bienveillant — comme quand on commente la journée au téléphone le \
lundi matin entre amis. Tu peux traiter quelqu'un de "boulet", se moquer d'une compo \
foireuse, célébrer un coup d'éclat avec ironie. Pas de langue de bois, pas de \
politiquement correct, mais sans méchanceté gratuite.

Les 8 joueurs : Raph, Nico, François, Damien, Greg, Marc, Pierre, Manu.

Mécanique des bonus mentionnés :
- "boostAllPlayers" (Zahia) : +0.5 à tous les titulaires
- "boostOnePlayer" (McDo) : +1 à un joueur ciblé
- "nerfGoalkeeper" (Suarez) : -1 au gardien adverse
- "nerfAllPlayers" (Cheat Code) : -0.5 à tous les adversaires sauf le gardien
- "removeGoal" (Valise à Nanard / Sifflet) : retire un but à l'adversaire
- "mirror" (Miroir) : retourne le bonus adverse contre lui
- "fourStrikers" (Décathlon) : 4-2-4 autorisé
- "blockTacticalSubs" (Tonton Pat') : bloque les remplacements adverses

Tu reçois les événements de la journée en JSON. Tu dois produire un résumé \
strictement au format JSON suivant (rien d'autre, juste le JSON brut) :

{
  "title": "Titre punchy de 5-12 mots qui résume L'événement le plus marquant de la journée",
  "summary_md": "3 à 5 puces markdown, chacune commençant par un emoji adapté, en français, ton charrieur, max 25 mots par puce."
}

Règles :
1. Le titre doit être PUNCHY et viser le truc le plus marquant. Priorité décroissante :
   champion sacré / chapeau scellé > record all-time > record de série battu > \
   bonus décisif > scores extrêmes.
2. Si rien de saillant : titre style "Journée tranquille, [nom du leader] continue de dominer".
3. Pour chaque puce, mentionne le joueur en gras (**Nom**) et l'événement avec ironie.
4. Pour les bonus décisifs : moque-toi du choix tactique (ou complimente avec sarcasme).
5. N'utilise PAS d'expressions clichées ("quelle journée !", "à suivre"). Sois spécifique.
6. Pas plus de 5 puces. Si plus de 5 événements, garde les plus drôles/marquants.
7. Garde un ton entre potes : "boulet", "claque magistrale", "chié dans son froc", \
   "sauvé par le bonus", "honte intersidérale", "pulvérise", etc.
"""


def _format_event_for_llm(ev: dict) -> str:
    """Sérialise un événement en ligne lisible pour le contexte LLM."""
    t = ev["type"]
    if t == "champion_sealed":
        return f"CHAMPION SCELLÉ : {ev['name']} mathématiquement sacré champion à J{ev['gw']} {ev['slabel']} ({ev['pts']} pts)."
    if t == "chapeau_sealed":
        return f"CHAPEAU SCELLÉ : {ev['name']} condamné à finir dernier à J{ev['gw']} {ev['slabel']} ({ev['pts']} pts)."
    if t == "record_alltime_gap":
        return (f"RECORD ALL-TIME ÉCART : {ev['winner']['name']} bat {ev['loser']['name']} "
                f"{ev['score']} (écart {ev['gap']} buts, ancien record {ev['old_gap']}).")
    if t == "record_h2h":
        return (f"RECORD H2H : {ev['winner']['name']} inflige {ev['score']} à {ev['loser']['name']} "
                f"(marge {ev['margin']}, ancien record {ev['old_margin']}).")
    if t == "gw_largest_gap":
        return (f"Plus large écart de la journée : {ev['winner']['name']} bat "
                f"{ev['loser']['name']} {ev['score']} (écart {ev['gap']}).")
    if t == "gw_most_goals":
        return (f"Match le plus prolifique : {ev['home']['name']} {ev['score']} "
                f"{ev['away']['name']} ({ev['total']} buts au total).")
    if t == "streak_record":
        if ev["kind"] == "approaching":
            need = {"win": "une victoire", "unbeaten": "un nul ou une victoire",
                    "loss": "une défaite"}.get(ev["category"], "un match")
            return (f"SÉRIE (à 1 du record) : {ev['name']} en est à {ev['value']} "
                    f"{ev['label']}, son record est {ev['old_record']} — encore {need} "
                    f"et il égalise (série depuis {ev['since']}).")
        verb = "égale" if ev["kind"] == "equaled" else "BAT"
        return (f"SÉRIE : {ev['name']} {verb} son record de {ev['label']} avec "
                f"{ev['value']} (ancien record {ev['old_record']}, depuis {ev['since']}).")
    if t == "decisive_bonus":
        out_map = {"W": "victoire", "D": "nul", "L": "défaite"}
        return (f"BONUS DÉCISIF ({ev['bonus_type']}) : {ev['user']['name']} vs "
                f"{ev['opponent']['name']} — score réel {ev['score_real']} ({out_map[ev['outcome_real']]}), "
                f"sans bonus {ev['score_sim']} ({out_map[ev['outcome_sim']]}).")
    return json.dumps(ev, ensure_ascii=False)


def _build_user_prompt(detection: dict) -> str:
    lines = [
        f"Journée {detection['game_week']} de la {detection['slabel']} (saison {detection['season']}).",
        "",
        "Événements détectés :",
    ]
    for ev in detection["events"]:
        lines.append(f"- {_format_event_for_llm(ev)}")
    lines.append("")
    lines.append("Produis le JSON {title, summary_md} attendu.")
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────────────
# LLM
# ──────────────────────────────────────────────────────────────────────────────

def _call_claude(detection: dict) -> Optional[dict]:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    try:
        from anthropic import Anthropic
    except ImportError:
        return None

    client = Anthropic(api_key=api_key)
    user_prompt = _build_user_prompt(detection)
    try:
        resp = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=1024,
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user_prompt}],
        )
    except Exception as e:
        print(f"[summary_writer] erreur Claude: {e}")
        return None

    text = "".join(b.text for b in resp.content if hasattr(b, "text"))
    # Extraire le bloc JSON (au cas où le modèle l'entoure de texte)
    match = re.search(r"\{[\s\S]*\}", text)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if "title" not in data or "summary_md" not in data:
        return None
    return data


# ──────────────────────────────────────────────────────────────────────────────
# Fallback templates statiques
# ──────────────────────────────────────────────────────────────────────────────

def _template_event_line(ev: dict) -> str:
    t = ev["type"]
    if t == "champion_sealed":
        return f"👑 **{ev['name']}** est mathématiquement champion ({ev['pts']} pts). C'est plié."
    if t == "chapeau_sealed":
        return f"🪣 **{ev['name']}** finit chapeau, c'est officiel ({ev['pts']} pts). Honte intersidérale."
    if t == "record_alltime_gap":
        return (f"💥 **{ev['winner']['name']}** colle un {ev['score']} à **{ev['loser']['name']}** "
                f"— record d'écart all-time pulvérisé (ancien : {ev['old_gap']}).")
    if t == "record_h2h":
        return (f"🔥 **{ev['winner']['name']}** détruit **{ev['loser']['name']}** {ev['score']} "
                f"— nouveau record H2H (ancien écart : {ev['old_margin']}).")
    if t == "gw_largest_gap":
        return (f"🎯 **{ev['winner']['name']}** roule sur **{ev['loser']['name']}** "
                f"({ev['score']}) — plus gros écart de la journée.")
    if t == "gw_most_goals":
        return (f"⚽ **{ev['home']['name']}** {ev['score']} **{ev['away']['name']}** "
                f"— {ev['total']} buts dans le même match.")
    if t == "streak_record":
        if ev["kind"] == "equaled":
            return f"✨ **{ev['name']}** égale son record de {ev['label']} ({ev['value']})."
        if ev["kind"] == "approaching":
            need = {"win": "une victoire", "unbeaten": "un nul ou une victoire",
                    "loss": "une défaite"}.get(ev["category"], "un match")
            return (f"👀 Encore {need} et **{ev['name']}** égale son record de "
                    f"{ev['label']} ({ev['old_record']}).")
        return (f"🚀 **{ev['name']}** explose son record de {ev['label']} : {ev['value']} "
                f"(ancien : {ev['old_record']}).")
    if t == "decisive_bonus":
        out_map = {"W": "gagne", "D": "fait nul", "L": "perd"}
        return (f"🎲 Bonus décisif de **{ev['user']['name']}** vs {ev['opponent']['name']} : "
                f"il {out_map[ev['outcome_real']]} {ev['score_real']}, "
                f"sans bonus il {out_map[ev['outcome_sim']]} {ev['score_sim']}.")
    return f"• {t}"


def _fallback_render(detection: dict) -> dict:
    events = detection["events"]
    if not events:
        return {
            "title":      f"Journée {detection['game_week']} {detection['slabel']} — RAS",
            "summary_md": "_Pas grand-chose à signaler cette journée. La routine._",
        }

    # Titre : on prend l'événement le plus prioritaire
    PRIORITY = [
        "champion_sealed", "chapeau_sealed",
        "record_alltime_gap", "record_h2h",
        "streak_record",
        "decisive_bonus",
        "gw_largest_gap", "gw_most_goals",
    ]
    sorted_events = sorted(
        events,
        key=lambda e: (
            PRIORITY.index(e["type"]) if e["type"] in PRIORITY else 99,
            -(e.get("value", 0) or e.get("gap", 0) or e.get("margin", 0)),
        ),
    )
    top = sorted_events[0]
    title_map = {
        "champion_sealed":  f"{top.get('name','?')} sacré champion mathématique",
        "chapeau_sealed":   f"{top.get('name','?')} chapeau, c'est plié",
        "record_alltime_gap": f"Record d'écart all-time pulvérisé",
        "record_h2h":       f"Nouveau record H2H signé {top.get('winner',{}).get('name','?')}",
        "streak_record":    (f"{top.get('name','?')} {'égale' if top.get('kind')=='equaled' else 'bat'} "
                             f"son record de {top.get('label','série')}"),
        "decisive_bonus":   f"Bonus décisif de {top.get('user',{}).get('name','?')}",
        "gw_largest_gap":   f"{top.get('winner',{}).get('name','?')} roule sur {top.get('loser',{}).get('name','?')}",
        "gw_most_goals":    f"Festival de buts : {top.get('score','')}",
    }
    title = title_map.get(top["type"], f"Journée {detection['game_week']} {detection['slabel']}")
    bullets = "\n".join(f"- {_template_event_line(ev)}" for ev in sorted_events[:5])
    return {"title": title, "summary_md": bullets}


# ──────────────────────────────────────────────────────────────────────────────
# API publique
# ──────────────────────────────────────────────────────────────────────────────

def write_summary(detection: dict, prefer_llm: bool = True) -> dict:
    """Renvoie {title, summary_md} pour une détection d'événements.

    Tente le LLM d'abord, puis tombe sur les templates si erreur ou clé absente.
    """
    if prefer_llm and detection.get("events"):
        result = _call_claude(detection)
        if result:
            return result
    return _fallback_render(detection)


if __name__ == "__main__":
    from event_detector import detect_all_events
    from mpg_db import get_conn

    with get_conn() as _c:
        det = detect_all_events(_c)
    if not det:
        print("Pas de journée terminée.")
    else:
        print("=== Détection ===")
        print(json.dumps(det, indent=2, ensure_ascii=False, default=str))
        print()
        print("=== Résumé ===")
        out = write_summary(det)
        print(f"Titre : {out['title']}")
        print(out['summary_md'])
