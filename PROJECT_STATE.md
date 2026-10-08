# PROJECT_STATE.md — MPG Assistant
> Version : 2.0 · Date : 2026-10-07 · Auteur initial : Claude Sonnet 4.6 · Mise à jour : Claude Sonnet 5.5

---

## TL;DR

Outil Python + SQLite d'analyse historique d'une ligue privée MPG (8 joueurs, depuis 2016).
Pipeline : fetch API → SQLite → analytics (standings / ELO / H2H / palmarès / séries / %chatte / recap post-journée) → pages HTML statiques dans `docs/` → GitHub Pages.
Branche annexe « Best Team » : compo optimale par manager, calculée en local par `bestteam_engine.py` (notes Ligue 1 en cache local) et injectée dans `bestteam.html` — plus de Supabase depuis octobre 2026 (projet mort).

**État actuel** (lecture de `mpg.db` le 2026-10-07, dernier sync le 2026-05-10) : 20 divisions importées (2016-2025), 1 084 matchs en DB dont 1 076 finalisés, **15 pages HTML** dans `docs/`, 13 + 8 tests (non relancés lors de cette mise à jour).
**Division en cours** : `_19_1` (S19, bascule faite le 2026-10-07, mercato en cours) — dérivée automatiquement de l'API ligue (`league.divisionsIds`), plus aucune constante MPG à changer (voir « Division courante : dérivée de l'API »).
**Site** : https://pifslicer-design.github.io/mpg-assistant/ (déclencheur : push sur `docs/`).
**Sync 100 % manuel** : aucun cron MPG n'est configuré dans WSL (le commentaire « Cron : lundi 7h » en tête de `sync_and_publish.sh` est obsolète).

---

## 1. STRUCTURE DES FICHIERS

```
mpg-assistant/
│  ── Cœur données ──
├── mpg_client.py          # CLI entry point (argparse), orchestration ; CurlClient (transport API MPG, headers navigateur)
├── mpg_db.py              # SQLite schema, migrations, UPSERT, exclusion filters, get_current_division() / COVID_DIVISIONS
├── mpg_fetchers.py        # HTTP thin layer : league / teams / matches (boucle GW, 404)
├── mpg_people.py          # Team name normalization + person_id resolution
├── people_mapping.yaml    # 8 players → aliases (team names per season) + display names
├── divisions.txt          # division_ids à synchroniser (sync-divisions) ; la courante est ajoutée en tête automatiquement si absente
├── mpg.db                 # SQLite WAL database (source of truth, non versionnée)
│
│  ── Analytics ──
├── mpg_legacy_engine.py   # All-time : standings, palmarès, ELO, H2H, compute_streaks (séries V/N/D)
├── mpg_stats.py           # Stats saison en cours (outcome dérivé des scores)
├── mpg_export.py          # JSON export, multi-scope, schema version 1
├── mpg_bonuses.py         # Rejeu de l'historique des bonus, stock restant, --bonus-advice (risque Miroir)
├── bonus_catalog.py       # Catalogue bonus : clé API → nom UI, stock par défaut, consommable ou non
├── mpg_goal_engine.py     # Reconstruction des buts virtuels MPG (simulation ligne par ligne) ; simulate_without_bonus()
│
│  ── Résumé post-journée (recaps) ──
├── event_detector.py      # Détecte les événements marquants de la dernière journée finalisée
│                          #   (champion/chapeau scellé, records all-time et saison, séries, scores extrêmes,
│                          #    chatte hebdo, bonus décisifs)
├── summary_writer.py      # Événements → résumé sarcastique : Claude Sonnet si ANTHROPIC_API_KEY, sinon templates
├── backfill_recaps.py     # Génère rétroactivement les recaps de journées passées (J1-J11 par défaut, --force)
│
│  ── Best Team (statique, local) ──
├── bestteam_engine.py     # Effectifs (teams.raw_json.squad) + notes L1 (mercato_cache/ratings_<saison>.json, refetch
│                          #   incrémental via l'API MPG) → score (decay 0.85, bonus buts/passes/domicile), formation,
│                          #   capitaine, commentaire Claude Haiku (cache mercato_cache/bestteam_commentary.json) ; L1_SEASON
├── mercato_cache/         # Cache local non versionné (créé par l'outil mercato privé, lu/rafraîchi par bestteam_engine)
│   ├── ratings_2026.json  #   notes L1 match par match ; index.json / pool.json / clubs.json : identités, cotes, clubs
│   └── l1_fixtures.json   #   affiche de la prochaine journée L1 (bonus domicile)
├── sync_l1_to_supabase.py # OBSOLÈTE (Supabase mort) — conservé pour référence, plus appelé
├── supabase/              # OBSOLÈTE — migration SQL + Edge Function TS (logique portée dans bestteam_engine.py)
├── run_lot1_migration.sh  # OBSOLÈTE
├── BESTTEAM_PROMPTS.md    # Prompts des sessions qui ont construit la première version (Supabase) de Best Team
│
│  ── Publication ──
├── generate_pages.py      # Régénère les pages HTML (injection de `const X = <json>` dans les gabarits), nav, miroir vers docs/
├── sync_and_publish.sh    # Pipeline manuel : .env → sync MPG → pages (dont Best Team) → git push docs/ → notif Gmail
├── notify.py              # Envoi Gmail via smtplib (credentials dans .env)
├── sync.log               # Log des exécutions (rotatif 500 lignes, non versionné)
│
│  ── Docs & tests ──
├── PROJECT_STATE.md       # Ce fichier
├── COMMANDES.md           # Commandes courantes
├── test_batch_import.py   # 8 tests d'intégration (import pipeline)
├── test_export.py         # Validation format/structure d'un export JSON
├── test_legacy_engine.py  # 13 tests analytics (palmarès, ELO, H2H, is_current)
│
└── docs/                  # 15 pages publiées sur GitHub Pages (miroir des *.html générés à la racine)
    ├── index.html                      # accueil : tuiles + résumé de la dernière journée
    ├── recaps.html                     # historique des résumés post-journée
    ├── classement_chronologique.html
    ├── classement_cumul.html
    ├── podiums.html
    ├── hall_of_fame.html
    ├── hall_of_shame.html
    ├── h2h.html
    ├── records.html
    ├── streaks.html                    # séries V/N/D, 3 modes + bandeau « Records en cours »
    ├── bonus_impact.html               # IMPACT / SORTED_BONUSES encore statiques (voir roadmap)
    ├── joueurs.html                    # stats joueurs de foot IRL + changements décisifs
    ├── bump.html                       # évolution des classements saison par saison
    ├── bestteam.html                   # compo optimale par manager (const BESTTEAM injectée, toggles indispo en JS)
    └── chatte.html                     # %chatte (méthode François) sur les buts IRL
```

Le dossier `site/` (copie obsolète de février 2026) a été supprimé : seul `docs/` est publié.

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
    is_current INT DEFAULT 0,     -- 1 = saison en cours (dérivée de league.divisionsIds, repli CURRENT_DIVISION)
    expected_matches INT DEFAULT 56,
    n_matches INT, gw_min INT, gw_max INT, notes TEXT
)

-- Cache des résumés post-journée (créée par generate_pages._ensure_recap_table)
journee_recap (
    season INT, division_id TEXT, game_week INT,
    events_json TEXT, summary_json TEXT, generated_at TEXT,
    PRIMARY KEY (season, division_id, game_week)
)

manifest (key TEXT PK, value TEXT)  -- tracking last GW fetched par division
league (id TEXT PK, name TEXT, mode TEXT, season INT, game_week_current INT, ...)
players (id TEXT PK, team_id TEXT, bid_date TEXT, price REAL, status INT, ...)

-- Index
idx_matches_gw ON matches(game_week)
idx_matches_season ON matches(season, division_id, game_week)
idx_matches_div_gw ON matches(division_id, game_week)
```

`sync_manifest.db` (base séparée, ignorée par git) : ancien état du sync Ligue 1 → Supabase (obsolète).
Best Team ne dépend plus d'aucune base distante : notes L1 dans `mercato_cache/ratings_<saison>.json` (format de l'outil mercato), effectifs dans `teams.raw_json.squad`.

---

## 3. LOGIQUE MÉTIER CLEF

### Terminologie

| Terme | Définition |
|---|---|
| **Division MPG** | `division_id` = 1 instance de ligue (8 équipes × 14 GW = 56 matchs) |
| **Saison IRL** | `divisions_metadata.season` = année civile (2016-2025). 2 divisions/an |
| **S1…S18** | Numéro extrait du `division_id` de la ligue Bédouins (`QU0SUZ6HQPB_<n>_1`) ; la ligue Numanuma (`PWN77AILXZQ`) s'affiche « Old S1 / Old S2 » (`slabel()` dans `generate_pages.py`) |
| **Saison en cours** | `is_current=1` → `_18_1` (S18) — à mettre à jour manuellement chaque saison |

### Dérivation de l'outcome (CRITIQUE)

```python
# mpg_legacy_engine.py — NE PAS utiliser finalResult (toujours = 1 dans l'API)
if home_score > away_score: outcome = 1  # home win
elif home_score < away_score: outcome = 3  # away win
else: outcome = 2  # draw
```

`mpg_stats.py` dérive désormais l'outcome des scores, comme `mpg_legacy_engine.py`.

### Matchs finalisés uniquement

`fetch_matches()` (mpg_legacy_engine.py) filtre `is_finalized=1` par défaut : une journée en cours contient des scores 0-0 fictifs qui fausseraient toutes les stats. Garder `True` partout dans les pages.

### Filtrage des divisions

```python
# get_excluded_divisions() dans mpg_db.py et list_included_divisions() dans mpg_legacy_engine.py
# Excluent par défaut : is_covid=1, is_incomplete=1, is_current=1
# → 18 divisions historiques complètes (16 Bédouins + 2 Old)
# Flags include_covid / include_incomplete / include_current pour les réintégrer.
# generate_pages.py filtre par `divisions` et `is_finalized` selon le type de page
# (bug GW13/14 corrigé — ne pas re-toucher sans comprendre).
```

### ELO

- Base : 1500, K-factor : 20, Zero-sum garanti
- Ordre déterministe : divisions dans l'ordre chronologique réel (`mpg_db.division_sort_key`, tri numérique du suffixe : S9 avant S10, jamais le tri texte SQL), puis `game_week ASC, match_id ASC`
- Vérification : avg(ELO) = 1499.99 ≈ 1500 ✓

### Résumé post-journée

`event_detector.detect_all_events()` → liste d'événements → `summary_writer.write_summary()` (Claude, repli sur templates) → mis en cache dans `journee_recap` → injecté dans `index.html` / `recaps.html` par `generate_index()`. `python3 generate_pages.py --regen-recap` force la régénération ; `backfill_recaps.py` rattrape les journées passées.

### Division courante : dérivée de l'API (plus de bascule manuelle)

Depuis octobre 2026, la division MPG en cours n'est plus codée en dur. Source de vérité : la réponse `/league/{LEAGUE_ID}` (table `league`, `raw_json`), champ `divisionsIds` — son dernier élément est la division courante (`mpg_db.get_current_division()` → `(division_id, source)`).

- `refresh_divisions_metadata()` pose `is_current=1` sur cette division. Repli sur la constante `CURRENT_DIVISION` uniquement si la table `league` est vide (DB neuve). Une division courante sans aucun match (intersaison) n'apparaît pas dans `divisions_metadata` : `event_detector.recap_division()` se rabat alors sur la dernière division jouée (comportement conservé).
- `mpg_client.py` : `DIVISION_ID` dans `.env` est **optionnel**. Priorité : `--division` > `DIVISION_ID` (.env, avec `[WARN]` s'il diffère de l'API) > `league.divisionsIds` > constante. Le `[CTX]` affiche la source retenue (`cli` / `env` / `league` / `constante`).
- `--sync-divisions` rafraîchit la ligue en premier, puis ajoute la division courante en tête du batch si elle manque dans `divisions.txt` (avec `[WARN]`).

**Seule étape manuelle restante (facultative)** au passage à la saison MPG suivante : ajouter la nouvelle `division_id` en tête de `divisions.txt` pour faire taire le `[WARN]`.

Constantes indépendantes de la bascule MPG : `COVID_DIVISIONS` (immuable) ; `L1_SEASON` dans `bestteam_engine.py` (saison Ligue 1 des notes Best Team, change une fois par an en août — voir la section Best Team).

---

## 4. ÉTAT DES DONNÉES (mpg.db local, lue le 2026-10-07 ; dernier sync 2026-05-10)

| Indicateur | Valeur |
|---|---|
| Divisions totales | 20 (18 Bédouins + 2 Old Numanuma) |
| Divisions historiques complètes | 18 (hors COVID, hors en cours) |
| Division COVID | 1 (_6_1, GW1-5 = 20 matchs) |
| Saison en cours | 1 (_18_1 / S18 — J1 à J12 finalisées, J13-J14 non finalisées en DB) |
| Matchs total DB | 1 084 (1 076 finalisés) |
| Matchs historiques (analytics) | 1 008 (18 × 56) |
| Équipes mappées (hors exclusions) | 144/144 — 100% |
| Joueurs uniques | 8 |

### Intégrité vérifiée (au 2026-02-20, non rejouée depuis)

| Check | Statut |
|---|---|
| Champion ≠ Chapeau par division | ✅ OK (19 divisions) |
| ELO zero-sum | ✅ OK (avg=1499.99) |
| 0 équipe non-mappée | ✅ OK |

### ELO rankings (CLI `--elo`, 18 divisions — is_current exclu, recalculé le 2026-10-07)

| # | Joueur | ELO | W | D | L |
|---|---|---|---|---|---|
| 1 | Raph | 1540.7 | 117 | 47 | 88 |
| 2 | Nico | 1526.9 | 114 | 46 | 92 |
| 3 | Greg | 1513.1 | 103 | 46 | 103 |
| 4 | Marc | 1510.5 | 92 | 42 | 118 |
| 5 | Manu | 1503.6 | 88 | 45 | 119 |
| 6 | Damien | 1492.5 | 108 | 50 | 94 |
| 7 | François | 1467.2 | 109 | 53 | 90 |
| 8 | Pierre | 1445.4 | 96 | 33 | 123 |

> 252 matchs/joueur = 18 divisions × 14 GW. Ces tableaux changeront quand S18 sera clôturée (elle entrera dans l'historique).

### Palmarès CLI (`--legacy`, 18 divisions — is_current exclu, recalculé le 2026-10-07)

| Joueur | Titres | Podiums | Chapeaux | Moy pts |
|---|---|---|---|---|
| François | 5 | 7 | 1 | 1.51 |
| Raph | 4 | 12 | 2 | 1.58 |
| Damien | 2 | 7 | 0 | 1.48 |
| Greg | 2 | 7 | 3 | 1.41 |
| Nico | 2 | 6 | 0 | 1.54 |
| Pierre | 1 | 6 | 4 | 1.27 |
| Marc | 1 | 5 | 6 | 1.26 |
| Manu | 1 | 4 | 2 | 1.23 |

---

## 5. BUGS CONNUS & RISQUES

### ✅ RÉSOLU — `list_included_divisions` ignorait `is_current`

**Fichier** : `mpg_legacy_engine.py:98`
**Fix appliqué** : paramètre `include_current: bool = False` + clause `is_current=0`.
**Test** : `test_current_exclusion_default()` dans `test_legacy_engine.py`.

### ✅ RÉSOLU — `mpg_stats.py` utilisait `finalResult`

Outcome dérivé de `home_score`/`away_score` (identique à `mpg_legacy_engine.py`). Matchs non finalisés (scores NULL) skippés proprement.
**Test** : `test_stats_wdl_coherence()` dans `test_batch_import.py`.

### ✅ RÉSOLU — Pages HTML avec données figées

`generate_pages.py` régénère toutes les pages (voir `PAGES`) ; lancer après chaque sync.
**Reste statique** : dans `bonus_impact.html`, seul `PLAYER_USAGE` est régénéré ; `IMPACT`/`SORTED_BONUSES` restent figés (simulation contrefactuelle non branchée — `mpg_goal_engine.simulate_without_bonus` existe et sert déjà à `event_detector`).

### ✅ RÉSOLU — GW13/14 des divisions historiques non finalisées (mai 2026)

Filtres `is_finalized` / `divisions` corrigés dans `generate_pages.py` et GW13/14 marquées finalisées sur les divisions historiques. Ne pas re-toucher sans comprendre.

### ✅ RÉSOLU — Constantes saisonnières hardcodées (oct. 2026)

La division courante est dérivée de `league.divisionsIds` (voir section 3, « Division courante : dérivée de l'API »). `DIVISION_ID` est optionnel et un `[WARN]` signale toute divergence avec l'API. Reste `divisions.txt`, complété automatiquement par le batch.

### 🟡 RISQUE — Token MPG expire ~24 h

`MPG_TOKEN` dans `.env` est un JWT à courte durée. Quand il expire, le sync renvoie des `401 Unauthorized`. Les divisions historiques déjà en DB ne sont pas affectées (pas de re-fetch), mais la division en cours n'est plus mise à jour.
**Action** : renouveler `MPG_TOKEN` (procédure dans `COMMANDES.md`) puis relancer le sync.
**Ne jamais écrire `.env` avec un outil d'édition** — patcher uniquement via Python WSL.

### 🟡 RISQUE — Aucune automatisation

Aucun cron MPG dans WSL : si personne ne lance `bash sync_and_publish.sh`, le site n'est pas mis à jour.

### 🟡 RISQUE — Secrets locaux

`.mcp.json` (token Supabase personnel en clair) est dans `.gitignore` et ne doit jamais être versionné. `.claude/settings.json` ne contient qu'une URL MCP publique (project_ref) et est versionné.

### 🟢 SOLIDE

- Idempotence UPSERT complète (pas de doublons)
- Outcome dérivé des scores (pas de l'API)
- 100% mapping person_id dans les divisions actives
- Tests champion ≠ chapeau, ELO zero-sum, H2H symétrie

---

## 6. ROADMAP

### Niveau 1 — Stabilisation

- [x] Fix `list_included_divisions` : ajouter `include_current=False` ✅
- [x] Fix `mpg_stats.py` : remplacer `finalResult` par comparaison de scores ✅
- [x] Script `generate_pages.py` centralisé — 15 pages ✅ + miroir auto vers `docs/`
- [x] Test `is_current` exclusion dans `test_legacy_engine.py` ✅
- [x] Fix `fetch_matches` : `is_finalized=1` par défaut (scores 0-0 fictifs) ✅
- [x] Hygiène dépôt : `site/` supprimé, `.mcp.json` ignoré, fichiers orphelins Best Team versionnés ✅
- [x] Division courante dérivée de l'API ligue (`get_current_division`) — plus de constante saisonnière MPG à changer ✅

### Niveau 2 — Analyse avancée

- [x] Séries V/N/D (plus longues séquences par joueur) ✅ — page `streaks.html`
- [x] streaks.html enrichie : 3 modes (fin S-prev / en cours / saison act.) + tooltips J/S + invaincu cross-saison ✅
- [x] Bandeau « Records en cours » sur streaks.html ✅
- [x] Page %Chatte (méthode François, buts IRL, all-time + heatmap, commentaire IA) ✅ — `chatte.html`
- [x] Résumé post-journée (event_detector + summary_writer + cache `journee_recap` + backfill) ✅ — `index.html` / `recaps.html`
- [x] Pages `joueurs.html` (stats joueurs IRL, changements décisifs) et `bump.html` ✅
- [ ] Forme récente (rolling average N dernières journées) — reporté
- [ ] Page ELO animé dans le temps
- [ ] Analyse home/away advantage (dom/ext)
- [ ] Contrefactuel `bonus_impact` (remplacer `IMPACT`/`SORTED_BONUSES` statiques par la simulation sans bonus)

### Niveau 3 — Avantage stratégique

- [x] Best Team : v1 Supabase (edge function) ✅ → v2 statique locale `bestteam_engine.py` (oct. 2026, Supabase mort) ✅
- [x] Conseil bonus pour la prochaine journée (`--bonus-advice`, catalogue `bonus_catalog.py`, risque Miroir) ✅
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

Détail dans `COMMANDES.md`. Tout se lance dans WSL, venv activé, depuis `~/mes-projets/mpg-assistant`.

```bash
# Sync toutes les divisions
python mpg_client.py --divisions-file divisions.txt --sync-divisions

# Analytics all-time (CLI)
python mpg_client.py --legacy        # standings + palmares
python mpg_client.py --elo           # classement ELO
python mpg_client.py --h2h raph nico # face-à-face
python mpg_client.py --streaks       # séries V/N/D all-time

# Stats saison en cours / bonus
python mpg_client.py --stats
python mpg_client.py --bonuses                      # bonus restants
python mpg_client.py --bonus-advice --no-fetch      # conseil bonus prochaine journée

# Export JSON
python mpg_client.py --export all

# Tests
python3 test_legacy_engine.py  # 13 tests
python3 test_batch_import.py   # 8 tests
python3 test_export.py <export.json>

# Régénération des pages HTML (copie auto dans docs/ à la fin du run)
python3 generate_pages.py                        # toutes les pages
python3 generate_pages.py podiums hall_of_fame   # pages spécifiques
python3 generate_pages.py --regen-recap          # force la régénération du résumé post-journée
python3 backfill_recaps.py                       # recaps rétroactifs (J1-J11 de la division courante)

# Best Team seule (notes L1 rafraîchies via l'API MPG si token valide ; BESTTEAM_NO_FETCH=1 pour rester hors ligne)
python3 generate_pages.py bestteam
python3 bestteam_engine.py --no-fetch --no-ai    # debug terminal

# Pipeline complet MANUEL (aucun cron) : .env → sync → pages → push docs/ → notif Gmail
bash sync_and_publish.sh
```

---

*Mis à jour le 2026-10-07 (hygiène dépôt + doc à jour) par Claude Sonnet 5.5*
