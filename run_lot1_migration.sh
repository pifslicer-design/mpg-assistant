#!/usr/bin/env bash
# run_lot1_migration.sh — Applique le Lot 1 (tables Supabase) via Claude + MCP supabase-mpg
#
# Prérequis :
#   - SUPABASE_ACCESS_TOKEN dans l'env (token personnel Supabase, pas le service key)
#     → https://supabase.com/dashboard/account/tokens
#   - claude CLI installé et authentifié
#
# Usage :
#   export SUPABASE_ACCESS_TOKEN=sbp_xxx
#   bash run_lot1_migration.sh

set -euo pipefail
cd "$(dirname "$0")"

if [[ -z "${SUPABASE_ACCESS_TOKEN:-}" ]]; then
  echo "[ERR] SUPABASE_ACCESS_TOKEN non défini."
  echo "      Génère un token sur https://supabase.com/dashboard/account/tokens"
  echo "      puis : export SUPABASE_ACCESS_TOKEN=sbp_xxx"
  exit 1
fi

SQL_FILE="supabase/migrations/lot1_bestteam_tables.sql"

if [[ ! -f "$SQL_FILE" ]]; then
  echo "[ERR] Fichier SQL introuvable : $SQL_FILE"
  exit 1
fi

echo "[INFO] Lancement migration Lot 1 via Claude + MCP supabase-mpg..."
echo "[INFO] Projet : rnqvttsqeonemxrapoiz"
echo

PROMPT=$(cat <<'EOF'
# Mission : Lot 1 Best Team — création tables Supabase MPG

Projet Supabase cible : rnqvttsqeonemxrapoiz

## Étape 1 — Lire le SQL
Lis le fichier `supabase/migrations/lot1_bestteam_tables.sql`.
Il contient 5 CREATE TABLE + RLS.

## Étape 2 — Appliquer en migrations séparées
Applique via `mcp__supabase-mpg__apply_migration`, **une migration par bloc logique** :

1. name="lot1_l1_players"         → CREATE TABLE l1_players + ENABLE RLS + POLICY
2. name="lot1_l1_player_ratings"  → CREATE TABLE l1_player_ratings + INDEX + ENABLE RLS + POLICY
3. name="lot1_mpg_rosters"        → CREATE TABLE mpg_rosters + ENABLE RLS + POLICY
4. name="lot1_l1_next_matches"    → CREATE TABLE l1_next_matches + ENABLE RLS + POLICY
5. name="lot1_mpg_schedule"       → CREATE TABLE mpg_schedule + ENABLE RLS + POLICY

Si une migration échoue avec "already exists" : c'est OK, passe à la suivante.
Si une migration échoue pour une autre raison : arrête et rapporte l'erreur exacte.

## Étape 3 — Vérifier
Appelle `mcp__supabase-mpg__list_tables` et confirme que les 5 tables sont présentes :
l1_players, l1_player_ratings, mpg_rosters, l1_next_matches, mpg_schedule

## Étape 4 — Rapport final
Indique pour chaque table : créée / déjà existante / erreur.
EOF
)

claude \
  --mcp-header "Authorization: Bearer ${SUPABASE_ACCESS_TOKEN}" \
  --allowedTools "mcp__supabase-mpg__apply_migration,mcp__supabase-mpg__list_tables,Read" \
  --print \
  "$PROMPT"
