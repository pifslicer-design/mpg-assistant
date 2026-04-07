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
Récupère les données des 2 dernières journées, régénère les 12 pages HTML, pousse sur GitHub Pages, envoie une notif Gmail.

⚠️ Nécessite un token MPG valide dans `.env`. Si erreur 401, renouveler le token (voir plus bas).

Puis lancer le sync Supabase (nécessaire pour le bestteam) :
```bash
set -a && source .env && set +a && python sync_l1_to_supabase.py
```
⚠️ `sync_and_publish.sh` ne charge pas `.env` dans le shell — ce step est toujours sauté automatiquement, il faut le lancer manuellement.

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
Fetche les 2 dernières journées pour chacune des 20 divisions. Utile après un renouvellement de token.

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
Régénère les 11 pages depuis la DB et les copie dans `docs/`.

```bash
python3 generate_pages.py podiums hall_of_fame
```
Régénère uniquement les pages spécifiées.

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
| Journée terminée, token OK | `bash sync_and_publish.sh` puis sync Supabase |
| Sync Supabase (bestteam) | `set -a && source .env && set +a && python sync_l1_to_supabase.py` |
| Token expiré (401) | Renouveler dans `.env`, puis `bash sync_and_publish.sh` |
| Données partielles à mettre à jour | `bash sync_and_publish.sh` — l'upsert met à jour automatiquement |
| Retard de 3+ journées | `python mpg_client.py --divisions-file divisions.txt --sync-divisions --force` |
| Conseil bonus prochaine journée | `python mpg_client.py --bonus-advice --no-fetch` |
