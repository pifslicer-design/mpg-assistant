#!/bin/bash
# sync_and_publish.sh — Sync MPG + régénération pages + publication GitHub Pages
# Cron : 0 7 * * 1  (lundi 7h00)

set -euo pipefail

PROJECT_DIR="/home/rapha/mes-projets/mpg-assistant"
LOG_FILE="$PROJECT_DIR/sync.log"
PYTHON="$PROJECT_DIR/.venv/bin/python3"
TIMESTAMP=$(date '+%Y-%m-%d %H:%M')

cd "$PROJECT_DIR"

# Rotation du log (garde les 500 dernières lignes)
if [[ -f "$LOG_FILE" ]]; then
    tail -500 "$LOG_FILE" > "${LOG_FILE}.tmp" && mv "${LOG_FILE}.tmp" "$LOG_FILE"
fi

# Tout rediriger vers le log
exec >> "$LOG_FILE" 2>&1

echo ""
echo "=== MPG Sync — $TIMESTAMP ==="

# Notification d'erreur en cas d'échec
on_error() {
    local exit_code=$?
    local line_no=$1
    local log_tail
    log_tail=$(tail -30 "$LOG_FILE" 2>/dev/null || echo "Log non disponible")
    echo "ERREUR ligne $line_no (code $exit_code) — envoi notification..."
    "$PYTHON" notify.py \
        "❌ MPG sync ERREUR — $TIMESTAMP" \
        "Erreur à la ligne $line_no (code de sortie : $exit_code)

--- Dernières lignes du log ---
$log_tail" || true
}
trap 'on_error $LINENO' ERR

# 1. Sync données
echo "[1/5] Sync divisions..."
"$PYTHON" mpg_client.py --divisions-file divisions.txt --sync-divisions

# 2. Sync Supabase (optionnel — nécessite SUPABASE_URL dans l'environnement)
if [[ -n "${SUPABASE_URL:-}" ]]; then
    echo "[2/5] Sync Supabase L1..."
    "$PYTHON" sync_l1_to_supabase.py
else
    echo "[2/5] Sync Supabase ignoré (SUPABASE_URL non défini)"
fi

# 3. Régénération pages HTML
echo "[3/5] Régénération pages..."
"$PYTHON" generate_pages.py

# 4. Commit + push si changements
echo "[4/5] Publication GitHub Pages..."
git add docs/
if git diff --staged --quiet; then
    echo "Aucun changement détecté dans docs/ — pas de commit."
    PUBLISHED=false
else
    git commit -m "chore: sync $(date +%Y-%m-%d)"
    git push
    PUBLISHED=true
fi

# 5. Notification succès
echo "[5/5] Envoi notification..."
if [[ "$PUBLISHED" == "true" ]]; then
    "$PYTHON" notify.py \
        "✅ MPG sync OK — $TIMESTAMP" \
        "Sync et publication réussis. Le site a été mis à jour."
else
    "$PYTHON" notify.py \
        "✅ MPG sync OK — $TIMESTAMP (aucun changement)" \
        "Sync terminé. Aucun changement détecté, le site n'a pas été republié."
fi

echo "=== Terminé — $TIMESTAMP ==="
