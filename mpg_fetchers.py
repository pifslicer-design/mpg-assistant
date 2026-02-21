"""MPG — fonctions de récupération API."""

import httpx
from mpg_db import save_league, save_teams, save_matches, set_manifest


def fetch_league(client: httpx.Client, league_id: str) -> dict:
    resp = client.get(f"/league/{league_id}")
    resp.raise_for_status()
    data = resp.json()
    save_league(data)
    return data


def fetch_teams(client: httpx.Client, division_id: str) -> list[dict]:
    resp = client.get(f"/teams/division/{division_id}")
    resp.raise_for_status()
    data = resp.json()

    # L'API peut retourner {"teams": [...]} ou directement une liste
    teams = data.get("teams") if isinstance(data, dict) else data
    save_teams(division_id, teams)
    return teams


def fetch_matches(client: httpx.Client, division_id: str, from_gw: int, to_gw: int) -> int:
    """Fetch les game-weeks [from_gw, to_gw] inclus. Retourne le nb de matchs sauvegardés."""
    manifest_key = f"last_non_empty_gw_saved::{division_id}"
    total = 0
    for gw in range(from_gw, to_gw + 1):
        url = f"/division/{division_id}/game-week/{gw}/matches"
        print(f"[HTTP] GET {url}")
        resp = client.get(url)

        if resp.status_code == 404:
            print(f"[SKIP] GW{gw} — 404")
            break

        if resp.status_code != 200 or not resp.content:
            ct = resp.headers.get("content-type", "?")
            print(f"[WARN] GW{gw} — status={resp.status_code} content-type={ct} body={resp.text[:200]}")
            break

        data = resp.json()
        top_keys = list(data.keys()) if isinstance(data, dict) else type(data).__name__
        matches = (
            data.get("divisionMatches")
            or data.get("matches")
            or (data if isinstance(data, list) else [])
        )
        print(f"[DEBUG] GW{gw} — status={resp.status_code} keys={top_keys} len={len(matches)}")

        if not matches:
            print(f"[SKIP] GW{gw} — divisionMatches absent ou vide")
            break

        save_matches(gw, matches, division_id)
        set_manifest(manifest_key, str(gw))
        total += len(matches)

    return total
