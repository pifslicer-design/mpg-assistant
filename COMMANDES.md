# Commandes MPG Assistant

> Toujours lancer depuis le dossier du projet avec le venv activé :
> ```bash
> cd ~/mes-projets/mpg-assistant
> source .venv/bin/activate
> ```

---

## Cas d'usage courants

### Après une journée MPG terminée
```bash
bash sync_and_publish.sh
```
Charge `.env`, récupère les données des 2 dernières journées, régénère les 15 pages HTML (dont Best Team, calculée en local), pousse `docs/` sur GitHub Pages, envoie une notif Gmail.

⚠️ Nécessite un token MPG valide dans `.env`. Si erreur 401, renouveler le token (voir plus bas).

ℹ️ Aucun cron MPG n'est configuré dans WSL : le sync est 100 % manuel.

ℹ️ Supabase n'est plus utilisé (projet mort, octobre 2026) : Best Team est désormais statique, voir plus bas.

---

### Mettre à jour le token MPG
1. Ouvrir mpg.football dans Chrome → F12 → Onglet Network
2. Naviguer dans l'appli → filtrer sur `api.mpg.football`
3. Copier le header `Authorization: Bearer <TOKEN>`
4. Mettre à jour `.env` : `MPG_TOKEN=<nouveau_token>`

Token valide ~24h.

---

### Sync manuel (sans republier)
```bash
python mpg_client.py
```
Fetche les 2 dernières journées de la division en cours uniquement.

---

### Sync toutes les divisions historiques
```bash
python mpg_client.py --divisions-file divisions.txt --sync-divisions
```
Fetche les 2 dernières journées pour chacune des divisions du fichier. Utile après un renouvellement de token.
La division courante (lue dans l'API ligue) est ajoutée en tête automatiquement si elle manque dans `divisions.txt`.

---

### Nouvelle saison MPG (septembre / février)
Rien à changer dans le code : la division courante est dérivée de l'API ligue (`league.divisionsIds`).
```bash
python mpg_client.py --divisions-file divisions.txt --sync-divisions
```
Le `[CTX] division courante=… (source=league)` confirme la bascule. Seule étape manuelle, facultative : ajouter la nouvelle `division_id` en tête de `divisions.txt` pour faire taire le `[WARN]`.

Vérifier la division retenue sans appel réseau :
```bash
python -c "from mpg_db import get_current_division; print(get_current_division())"
```
`DIVISION_ID` dans `.env` est optionnel ; s'il est présent et diffère de l'API, un `[WARN]` le signale (le `.env` l'emporte).

---

### Forcer le re-fetch complet (toutes les journées)
```bash
python mpg_client.py --divisions-file divisions.txt --sync-divisions --force
```
À utiliser si tu as raté 3+ journées consécutives. Plus lent (refetch tout l'historique).

---

### Conseil bonus pour la prochaine journée
```bash
python mpg_client.py --bonus-advice --no-fetch
```
Affiche tes bonus restants, ceux de l'adversaire, son historique de bonus contre toi, et une recommandation tenant compte du risque Miroir.

---

### Voir les résultats de la saison en cours
```bash
python mpg_client.py --results
```
Affiche tous les scores de la saison, journée par journée.

```bash
python mpg_client.py --results 7
```
Affiche uniquement la J7.

---

### Classement ELO all-time
```bash
python mpg_client.py --elo
```
Classement ELO sur les 18 saisons historiques (saison en cours exclue).

---

### Palmarès + ELO all-time
```bash
python mpg_client.py --legacy
```
Affiche palmarès (titres, podiums, chapeaux) + classement ELO.

---

### Head-to-head entre deux joueurs
```bash
python mpg_client.py --h2h raph nico
python mpg_client.py --h2h francois marc
```
Stats face-à-face all-time entre deux joueurs.

---

### Séries V/N/D all-time
```bash
python mpg_client.py --streaks
```
Meilleures séries de victoires/nuls/défaites par joueur.

---

### Régénérer les pages HTML sans sync
```bash
python3 generate_pages.py
```
Régénère les 15 pages depuis la DB et les copie dans `docs/`.

```bash
python3 generate_pages.py podiums hall_of_fame
```
Régénère uniquement les pages spécifiées.

---

### Best Team (compo optimale par manager, statique)
```bash
python3 generate_pages.py bestteam
```
`bestteam_engine.py` lit les effectifs dans `mpg.db` (`teams.raw_json.squad`, division courante), les notes L1 dans `mercato_cache/ratings_2026.json` (rafraîchies via l'API MPG si le token est valide, sinon cache tel quel), calcule score / formation / capitaine (même logique que l'ancienne Edge Function) et injecte le tout dans `bestteam.html`. Commentaire Claude Haiku si `ANTHROPIC_API_KEY` est dans `.env` (cache `mercato_cache/bestteam_commentary.json`), gabarit local sinon.

```bash
BESTTEAM_NO_FETCH=1 python3 generate_pages.py bestteam   # sans appel à l'API MPG (cache local)
python3 bestteam_engine.py --no-fetch --no-ai            # debug : affiche les 8 compos dans le terminal
```
Les toggles « Indispo » et le choix de formation sont recalculés dans le navigateur, sans réseau.

---

### Publier manuellement sur GitHub Pages
```bash
git add docs/ && git commit -m "chore: sync $(date +%Y-%m-%d)" && git push
```

---

### Diagnostic base de données
```bash
python mpg_client.py --doctor
```
Affiche la distribution des matchs par division, les bonus utilisés, l'état des métadonnées.

---

### Export JSON
```bash
python mpg_client.py --export export.json
python mpg_client.py --export export.json --pretty
```

---

### Tests
```bash
python3 test_legacy_engine.py   # analytics all-time (13 tests)
python3 test_batch_import.py    # pipeline import (8 tests)
python3 test_export.py export.json
```

---

## Rappels

| Situation | Commande |
|---|---|
| Journée terminée, token OK | `bash sync_and_publish.sh` |
| Régénérer seulement Best Team | `python3 generate_pages.py bestteam` |
| Automatisation | Aucun cron MPG dans WSL — tout est lancé à la main |
| Token expiré (401) | Renouveler dans `.env`, puis `bash sync_and_publish.sh` |
| Données partielles à mettre à jour | `bash sync_and_publish.sh` — l'upsert met à jour automatiquement |
| Retard de 3+ journées | `python mpg_client.py --divisions-file divisions.txt --sync-divisions --force` |
| Nouvelle saison MPG | Rien à coder — `bash sync_and_publish.sh` ; ajouter la division dans `divisions.txt` pour faire taire le `[WARN]` |
| Conseil bonus prochaine journée | `python mpg_client.py --bonus-advice --no-fetch` |
