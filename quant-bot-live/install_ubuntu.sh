#!/usr/bin/env bash
set -euo pipefail

echo "========================================================================"
echo " QUANT-BOT-LIVE v3.0 — OTOMATIK UBUNTU SUNUCU KURULUM SCRIPTI"
echo "========================================================================"

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CURRENT_USER="$(whoami)"
CURRENT_GROUP="$(id -gn)"

echo "[1/6] Sistem paketleri ve Chrony NTP servisi kuruluyor..."
sudo apt-get update -y
sudo apt-get install -y curl git build-essential chrony
sudo systemctl enable --now chrony

echo "[2/6] uv Python paket yoneticisi ve Python 3.12 sanal ortami (.venv) hazirlaniyor..."
if ! command -v uv >/dev/null 2>&1; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi

cd "$APP_DIR"
"$HOME/.local/bin/uv" venv --python 3.12 .venv || uv venv --python 3.12 .venv
"$HOME/.local/bin/uv" pip install -r requirements.txt || uv pip install -r requirements.txt

echo "[3/6] .env yapılandırma dosyası kontrol ediliyor..."
if [ ! -f "$APP_DIR/.env" ]; then
    cp "$APP_DIR/.env.example" "$APP_DIR/.env"
    echo "  [UYARI] .env dosyasi .env.example'dan olusturuldu. Lutfen API anahtarlarinizi kontrol edin."
else
    echo "  [OK] Mevcut .env dosyasi bulundu."
fi
mkdir -p "$APP_DIR/data" "$APP_DIR/logs"

echo "[4/6] Mevcut servis calisiyorsa durduruluyor (DuckDB kilit cakismasini onlemek icin)..."
sudo systemctl stop quant-bot.service 2>/dev/null || true

echo "[5/6] Birim testleri (pytest) ve Pre-Flight API kontrolleri calistiriliyor..."
./.venv/bin/pytest -v
./.venv/bin/python preflight_check.py

echo "[6/6] Systemd servisi (/etc/systemd/system/quant-bot.service) kuruluyor ve baslatiliyor..."
sudo tee /etc/systemd/system/quant-bot.service > /dev/null <<EOF
[Unit]
Description=Autonomous Quantitative Trading Engine v3.0 & Protected Web Dashboard
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${CURRENT_USER}
Group=${CURRENT_GROUP}
WorkingDirectory=${APP_DIR}
EnvironmentFile=${APP_DIR}/.env
ExecStart=${APP_DIR}/.venv/bin/python ${APP_DIR}/main.py
Restart=always
RestartSec=5
StandardOutput=append:${APP_DIR}/logs/systemd.log
StandardError=append:${APP_DIR}/logs/systemd.log

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now quant-bot.service

echo "========================================================================"
echo " KURULUM TAMAMLANDI! Servis durumu:"
echo "========================================================================"
sudo systemctl status quant-bot.service --no-pager -l
