#!/usr/bin/env bash
# ==============================================================================
# Safe Deployment Script for QuantBot (Phase F1 / R5)
# Performs:
# 1. Full pytest suite run
# 2. Preflight environment and clock drift verification
# 3. Position safety verification (aborts if unprotected positions exist)
# 4. DuckDB atomic snapshot backup
# 5. Clean systemd service restart
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

cd "${BOT_DIR}"

echo "=== [1/5] Running Automated Test Suite ==="
python -m pytest -q

echo "=== [2/5] Running Preflight Verification ==="
python preflight_check.py

echo "=== [3/5] Verifying Open Positions & Exposure Guardrails ==="
python -c '
from database.db_manager import DatabaseManager
db = DatabaseManager()
open_positions = db.get_open_positions(include_unprotected=True)
unprotected = [p for p in open_positions if p.get("status") == "OPEN_UNPROTECTED" or not p.get("sl_exchange_order_id")]
if unprotected:
    print(f"CRITICAL: Found {len(unprotected)} unprotected position(s)! Aborting deploy to prevent risk.")
    for p in unprotected:
        print(f" - Symbol: {p.get(\"symbol\")} | ID: {p.get(\"position_id\")}")
    exit(1)
print(f"Position safety check passed ({len(open_positions)} protected open positions).")
'

echo "=== [4/5] Creating Database Backup ==="
BACKUP_DIR="${BOT_DIR}/data/backups"
mkdir -p "${BACKUP_DIR}"
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
python -c "
import shutil, os
from config.settings import settings
db_file = settings.effective_db_path
if os.path.exists(db_file):
    backup_file = '${BACKUP_DIR}/' + os.path.basename(db_file) + '.${TIMESTAMP}.bak'
    shutil.copy2(db_file, backup_file)
    print(f'Database backed up to {backup_file}')
"

echo "=== [5/5] Restarting Service ==="
if command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet quant-bot.service 2>/dev/null; then
    echo "Restarting quant-bot.service..."
    sudo systemctl restart quant-bot.service
    echo "Deployment completed successfully! Service is active."
else
    echo "quant-bot.service is not active or systemctl not available. Deploy completed without restart."
fi
