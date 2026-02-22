# PROJECT_STATE.md — MPG Assistant
> Version : 1.0 · Date : 2026-02-20 · Auteur : Claude Sonnet 4.6

---

## TL;DR

Outil Python + SQLite d'analyse historique d'une ligue privée MPG (8 joueurs, depuis 2016).
Pipeline : fetch API → SQLite → analytics (standings / ELO / H2H / palmares) → pages HTML statiques.

**État actuel** : 20 divisions importées (2016-2025), 1 084 matchs en DB, 13/13 + 8/8 tests passants.
**Bugs résolus** : `list_included_divisions()` filtre `is_current` par défaut · `mpg_stats.py` dérive l'outcome des scores (plus de dépendance à `finalResult`).

---

## 1. STRUCTURE DES FICHIERS

```
mpg-assistant/
├── mpg_client.py          # CLI entry point (argparse), orchestration, state
├── mpg_db.py              # SQLite schema, migrations, UPSERT, exclusion filters
├── mpg_fetchers.py        # HTTP thin layer (httpx), GW loop, 404 handling
├── mpg_legacy_engine.py   # All-time analytics : standings, palmares, ELO, H2H
├── mpg_stats.py           # Current season stats (uses finalResult from API JSON)
├── mpg_export.py          # JSON export, multi-scope, schema version 1
├── mpg_people.py          # Team name normalization + person_id resolution
├── mpg_bonuses.py         # Bonus consumption replay, remaining stock
├── bonus_catalog.py       # 8 consumable + 3 permanent bonus definitions
├── people_mapping.yaml    # 8 players → aliases (team names per season)
├── divisions.txt          # 20 division_ids to sync
├── mpg.db                 # SQLite WAL database (source of truth)
├── test_batch_import.py   # 7 integration tests (import pipeline)
├── test_export.py         # Export format/structure validation
├── test_legacy_engine.py  # 11 analytics tests (palmares, ELO, H2H)
└── *.html                 # 6 static HTML pages with inline JSON data
    ├── index.html
    ├── classement_chronologique.html
    ├── classement_cumul.html
    ├── podiums.html
    ├── hall_of_fame.html
    ├── hall_of_shame.html
    └── h2h.html
```

---

## 2. SCHÉMA BASE DE DONNÉES

```sql
-- Source de vérité principale
matches (
    id TEXT PK, game_week INT, season INT, division_id TEXT,
    home_team_id TEXT, away_team_id TEXT,
    home_score REAL, away_score REAL,      -- scores MPG (74.5, etc.)
    home_bonuses TEXT, away_bonuses TEXT,   -- JSON
    is_finalized INT DEFAULT 0,
    raw_json TEXT, fetched_at TEXT
)

teams (
    id TEXT PK, division_id TEXT, name TEXT,
    user_id TEXT, budget REAL, person_id TEXT,  -- mappé via people_mapping.yaml
    raw_json TEXT, fetched_at TEXT
)

divisions_metadata (
    division_id TEXT PK, season INT,
    is_covid INT DEFAULT 0,       -- 1 = division COVID (hardcodée)
    is_incomplete INT DEFAULT 0,  -- 1 = n_matches < expected_matches (56)
    is_current INT DEFAULT 0,     -- 1 = saison en cours (hardcodée)
    expected_matches INT DEFAULT 56,
    n_matches INT, gw_min INT, gw_max INT, notes TEXT
)

manifest (key TEXT PK, value TEXT)  -- tracking last GW fetched par division
league (id TEXT PK, name TEXT, mode TEXT, season INT, game_week_current INT, ...)
players (id TEXT PK, team_id TEXT, bid_date TEXT, price REAL, status INT, ...)

-- Index
idx_matches_gw ON matches(game_week)
idx_matches_season ON matches(season, division_id, game_week)
idx_matches_div_gw ON matches(division_id, game_week)
```

---

## 3. LOGIQUE MÉTIER CLEF

### Terminologie

| Terme | Définition |
|---|---|
| **Division MPG** | `division_id` = 1 instance de ligue (8 équipes × 14 GW = 56 matchs) |
| **Saison IRL** | `divisions_metadata.season` = année civile (2016-2025). 2 divisions/an |
| **S1…S18** | Numérotation ordinale des divisions historiques dans les HTML (calculé) |
| **Saison en cours** | `is_current=1` → _18_1 (à mettre à jour manuellement chaque saison) |

### Derivation de l'outcome (CRITIQUE)

```python
# mpg_legacy_engine.py — NE PAS utiliser finalResult (toujours = 1 dans l'API)
if home_score > away_score: outcome = 1  # home win
elif home_score < away_score: outcome = 3  # away win
else: outcome = 2  # draw
```

⚠️ `mpg_stats.py` utilise `finalResult` du JSON API (potentiellement incorrect).

### Filtrage des divisions

```python
# get_excluded_divisions() dans mpg_db.py — utilisé par les pages HTML
# Exclut par défaut : is_covid=1, is_incomplete=1, is_current=1
# → 18 divisions historiques complètes

# list_included_divisions() dans mpg_legacy_engine.py — utilisé par le CLI
# Exclut par défaut : is_covid=1, is_incomplete=1
# ⚠️ NE filtre PAS is_current → inclut la saison en cours !
```

### ELO

- Base : 1500, K-factor : 20, Zero-sum garanti
- Ordre déterministe : `season ASC, division_id ASC, game_week ASC, match_id ASC`
- Vérification : avg(ELO) = 1499.99 ≈ 1500 ✓

### Constantes à mettre à jour à chaque nouvelle saison

```python
# mpg_db.py
COVID_DIVISIONS = frozenset({"mpg_division_QU0SUZ6HQPB_6_1"})  # immuable
CURRENT_DIVISION = "mpg_division_QU0SUZ6HQPB_18_1"              # ← À CHANGER
```

---

## 4. ÉTAT DES DONNÉES (au 2026-02-20)

| Indicateur | Valeur |
|---|---|
| Divisions totales | 20 |
| Divisions historiques complètes | 18 (hors COVID, hors en cours) |
| Division COVID | 1 (_6_1, GW1-5 = 20 matchs) |
| Saison en cours | 1 (_18_1, 8/56 matchs scorés ≈ GW2) |
| Matchs total DB | 1 084 |
| Matchs historiques (analytics) | 1 008 (18 × 56) |
| Matchs avec scores | 1 036 |
| Équipes mappées (hors exclusions) | 144/144 — 100% |
| Joueurs uniques | 8 |

### Intégrité vérifiée

| Check | Statut |
|---|---|
| Champion ≠ Chapeau par division | ✅ OK (19 divisions) |
| ELO zero-sum | ✅ OK (avg=1499.99) |
| 0 équipe non-mappée | ✅ OK |
| 11/11 tests | ✅ Passants |

### ELO rankings (CLI, 19 saisons)

| # | Joueur | ELO | W | D | L |
|---|---|---|---|---|---|
| 1 | Raph | 1537.7 | 118 | 47 | 89 |
| 2 | Marc | 1520.6 | 93 | 43 | 118 |
| 3 | Nico | 1516.9 | 114 | 47 | 93 |
| 4 | Greg | 1510.9 | 104 | 46 | 104 |
| 5 | Damien | 1509.9 | 110 | 50 | 94 |
| 6 | Manu | 1504.1 | 88 | 47 | 119 |
| 7 | François | 1470.6 | 110 | 53 | 91 |
| 8 | Pierre | 1429.2 | 96 | 33 | 125 |

> ⚠️ Marc en 2e ELO malgré bilan négatif (93W/118L) — artefact à investiguer.

### Palmares CLI (18 saisons — is_current exclu, corrigé)

| Joueur | Titres | Chapeaux |
|---|---|---|
| François | 5 | 1 |
| Raph | 4 | 2 |
| Damien | 2 | 0 |
| Greg | 2 | 3 |
| Nico | 2 | 0 |
| Marc | 1 | 6 |
| Pierre | 1 | 5 |
| Manu | 1 | 2 |

---

## 5. BUGS CONNUS & RISQUES

### ✅ RÉSOLU — `list_included_divisions` ignorait `is_current`

**Fichier** : `mpg_legacy_engine.py:98`
**Fix appliqué** : Ajout du paramètre `include_current: bool = False` + clause `is_current=0`.
**Résultat** : Damien = 2 titres (correct). ELO recalculé sur 18 divisions. 12/12 tests passants.
**Test** : `test_current_exclusion_default()` dans `test_legacy_engine.py`.

### ✅ RÉSOLU — `mpg_stats.py` utilisait `finalResult`

**Fix appliqué** : Outcome dérivé de `home_score`/`away_score` (identique à `mpg_legacy_engine.py`).
Matchs non finalisés (scores NULL) skippés proprement.
**Test** : `test_stats_wdl_coherence()` dans `test_batch_import.py`.

### 🟡 RISQUE — Pages HTML avec données figées

Les HTML contiennent les données en JSON inline. Pas de régénération automatique si DB mise à jour.
**Fix** : Script `generate_pages.py` centralisé.

### 🟡 RISQUE — Constantes saisonnières hardcodées

`CURRENT_DIVISION` dans `mpg_db.py` doit être mis à jour manuellement chaque saison.
Pas de validation que la valeur correspond à une division en DB.

### 🟢 SOLIDE

- Idempotence UPSERT complète (pas de doublons)
- Outcome dérivé des scores (pas de l'API) dans legacy_engine
- 100% mapping person_id dans les divisions actives
- Tests champion ≠ chapeau, ELO zero-sum, H2H symétrie

---

## 6. ROADMAP

### Niveau 1 — Stabilisation (prioritaire)

- [x] Fix `list_included_divisions` : ajouter `include_current=False` ✅
- [x] Fix `mpg_stats.py` : remplacer `finalResult` par comparaison de scores ✅
- [x] Script `generate_pages.py` centralisé — classement_cumul + classement_chronologique ✅
- [x] Test `is_current` exclusion dans `test_legacy_engine.py` ✅

### Niveau 2 — Analyse avancée

- [x] Séries V/N/D (plus longues séquences par joueur) ✅
- [ ] Forme récente (rolling average N dernières journées)
- [ ] Page ELO animé dans le temps
- [ ] Analyse home/away advantage

### Niveau 3 — Avantage stratégique

- [ ] Prédicteur de match (ELO + H2H + forme)
- [ ] Optimiseur d'enchères
- [ ] Alerte bonus adversaire

---

## 7. JOUEURS & ÉQUIPES

| person_id | Display | Aliases historiques |
|---|---|---|
| raph | Raph | San Chapo FC, San Chapo, issy ci boubou |
| manu | Manu | PIMPAMRAMI, Olympique de McCourt |
| pierre | Pierre | Lulu FC, Lulu Football Club |
| damien | Damien | Stade Malherbe Milan, Stade Malherbe de Milan |
| francois | François | Chien Chaud, Chien Chaud FC, Les Malabars |
| marc | Marc | FC Miller, Miler FC, Miller FC |
| greg | Greg | Cup, NAPPY FC |
| nico | Nico | Puntagliera, Punta |

---

## 8. COMMANDES CLEF

```bash
# Sync toutes les divisions
python mpg_client.py --sync-divisions divisions.txt

# Analytics all-time (CLI)
python mpg_client.py --legacy        # standings + palmares
python mpg_client.py --elo           # classement ELO
python mpg_client.py --h2h raph nico # face-à-face

# Stats saison en cours
python mpg_client.py --stats

# Export JSON
python mpg_client.py --export all

# Tests
python test_legacy_engine.py
python test_batch_import.py
python test_export.py

# Régénération pages HTML (après chaque sync)
# ⚠️ Pas encore de script centralisé — fait manuellement
```

---

*Généré le 2026-02-20 par Claude Sonnet 4.6 — ne pas modifier manuellement*
