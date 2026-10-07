# Best Team — Prompts d'implémentation

## Session Opus — Superviseur

```
Tu supervises l'implémentation de la feature "Best Team" du projet MPG Assistant à /home/rapha/mes-projets/mpg-assistant/

Lis ces fichiers pour te mettre à jour :
1. Plan détaillé : /home/rapha/.claude/plans/zesty-prancing-yao.md
2. Mémoire projet : /home/rapha/.claude/projects/-mnt-c-Users-rapha/memory/mpg-bestteam.md
3. Ce fichier de prompts : /home/rapha/mes-projets/mpg-assistant/BESTTEAM_PROMPTS.md

L'implémentation est découpée en 5 lots. On lance chaque lot dans une session dédiée (Sonnet ou Opus selon le lot). Ton rôle : piloter l'ordre, valider entre chaque lot, résoudre les blocages.

Supabase MPG : projet rnqvttsqeonemxrapoiz — MCP `supabase-mpg` (scope projet)
⚠️ Le MCP global `supabase` pointe vers PIF (autre orga) — NE PAS l'utiliser pour MPG.

Lots :
- Lot 1 (Sonnet) : Supabase setup — tables PostgreSQL + RLS
- Lot 2 (Sonnet) : Script sync Python — sync_l1_to_supabase.py
- Lot 3 (Opus) : Edge Function Supabase — scoring + Haiku
- Lot 4 (Sonnet) : Frontend — bestteam.html
- Lot 5 (Sonnet) : Intégration workflow — nav, sync_and_publish.sh
```

---

## Session Sonnet — Lot 1 + 2 (Supabase setup + sync script)

```
Tu travailles sur le projet MPG Assistant à /home/rapha/mes-projets/mpg-assistant/

Lis le plan d'implémentation : /home/rapha/.claude/plans/zesty-prancing-yao.md
Lis la mémoire projet : /home/rapha/.claude/projects/-mnt-c-Users-rapha/memory/mpg-bestteam.md

⚠️ IMPORTANT — MCP Supabase :
- Utilise UNIQUEMENT les outils `mcp__supabase-mpg__*` (projet MPG : rnqvttsqeonemxrapoiz)
- NE PAS utiliser `mcp__supabase__*` qui pointe vers un autre projet (PIF)
- Si le MCP demande une authentification OAuth, suis le flow

Tu dois implémenter les Lots 1 et 2.

**Lot 1 — Supabase setup** :
- Crée les 5 tables PostgreSQL via `mcp__supabase-mpg__apply_migration`
- Configure RLS lecture publique sur chaque table
- SQL exact dans le plan (section Lot 1)
- Vérifie avec `mcp__supabase-mpg__list_tables`

**Lot 2 — Script sync Python** :
- Crée sync_l1_to_supabase.py à la racine du projet
- Réutilise build_client() de mpg_client.py pour le token MPG
- Utilise httpx pour pousser vers Supabase REST API (pas de SDK Supabase)
- Implémente les 5 étapes de sync décrites dans le plan
- Supporte --full (backfill) et sync incrémental par défaut
- Supporte --season YYYY --gw START END pour un range spécifique
- Tracking incrémental via table manifest SQLite locale
- Les variables SUPABASE_URL et SUPABASE_SERVICE_KEY sont dans .env (utilise dotenv)
- Teste avec : python sync_l1_to_supabase.py (incrémental d'abord, puis --full si ça marche)

Contraintes :
- Ne modifie aucun fichier existant dans ce lot
- Ne crée que sync_l1_to_supabase.py
- Suis les patterns httpx existants du projet (lis mpg_client.py pour comprendre)
```

---

## Session Opus — Lot 3 (Edge Function)

```
Tu travailles sur le projet MPG Assistant à /home/rapha/mes-projets/mpg-assistant/

Lis le plan d'implémentation : /home/rapha/.claude/plans/zesty-prancing-yao.md
Lis la mémoire projet : /home/rapha/.claude/projects/-mnt-c-Users-rapha/memory/mpg-bestteam.md

⚠️ MCP Supabase : utilise `mcp__supabase-mpg__*` (projet MPG), PAS `mcp__supabase__*` (PIF)

Tu dois implémenter le Lot 3 — Edge Function Supabase "best-team".

La fonction doit :
1. Recevoir {person_id, unavailable[], window} en POST
2. Charger le roster du joueur + ses ratings L1 depuis les tables Supabase
3. Calculer un score composite par joueur (decay exponentiel 0.85, bonus goals/assists, bonus dom/ext)
4. Trouver la formation optimale (G/D/M/A, formations: 343/352/433/442/451/532/541)
5. Choisir le capitaine optimal (meilleur score + averageRating > 6)
6. Appeler Claude Haiku pour un commentaire tactique (ton décontracté entre potes, 3-4 phrases)
7. Retourner le JSON de réponse décrit dans le plan

Flags données limitées : <3 matchs → "⚠️", 3-5 → "données limitées", >5 → normal

Deploy avec : supabase functions deploy best-team
Secrets à configurer : ANTHROPIC_API_KEY

Teste avec curl après deploy.
```

---

## Session Sonnet — Lot 4 + 5 (Frontend + intégration)

```
Tu travailles sur le projet MPG Assistant à /home/rapha/mes-projets/mpg-assistant/

Lis le plan d'implémentation : /home/rapha/.claude/plans/zesty-prancing-yao.md
Lis la mémoire projet : /home/rapha/.claude/projects/-mnt-c-Users-rapha/memory/mpg-bestteam.md

Supabase MPG : URL = https://rnqvttsqeonemxrapoiz.supabase.co

Tu dois implémenter les Lots 4 et 5.

**Lot 4 — Frontend bestteam.html** :
- Crée bestteam.html à la racine du projet
- Suis EXACTEMENT le design system existant : lis joueurs.html et index.html comme référence (CSS variables, Nunito, cards, nav sticky)
- Sections : header gradient → dropdown équipe → prochain match MPG → roster avec toggles indispo → bouton Générer → résultat (formation + terrain CSS + capitaine + banc + commentaire IA)
- Vanilla JS, pas de framework
- Les constantes SUPABASE_URL et SUPABASE_ANON_KEY sont en dur dans le HTML
- Responsive mobile-first

**Lot 5 — Intégration** :
- Dans generate_pages.py : ajoute "🧠 Best Team" dans NAV_HTML + enregistre bestteam.html dans le registre PAGES pour copie vers docs/
- Dans sync_and_publish.sh : ajoute l'étape sync Supabase (conditionnel sur SUPABASE_URL)
- Teste le workflow complet : generate_pages.py → docs/ → vérification

Contraintes :
- Ne touche que generate_pages.py et sync_and_publish.sh comme fichiers existants
- Lis d'abord les fichiers HTML existants pour capter le style exact
```
