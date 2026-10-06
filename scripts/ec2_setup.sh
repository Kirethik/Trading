#!/bin/bash
# EC2 Setup Script — runs ON the EC2 instance after code deployment
# Target: Amazon Linux 2023 (dnf-based).
# Idempotent: safe to re-run on updates.

set -e

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

echo "======================================================================="
echo "Regime-Trader EC2 Setup (Amazon Linux 2023)"
echo "======================================================================="
echo ""

# ================================================================
# 1. System dependencies (Python, Node, Redis, nginx, cronie)
# ================================================================
echo "[...] Installing system packages..."
# nodejs20 and redis6 are the AL2023 package names (not nodejs/redis7)
sudo dnf install -y -q python3.11 python3.11-pip git cronie nginx nodejs20 redis6

sudo systemctl enable --now crond
echo "[✓] System packages installed (node: $(node --version), npm: $(npm --version))"

# ================================================================
# 2. Redis
# ================================================================
echo "[...] Enabling Redis (redis6)..."
sudo systemctl enable --now redis6
sleep 1
echo "[✓] Redis running ($(redis6-cli ping 2>/dev/null || echo 'started'))"

# ================================================================
# 3. Python virtual environment + dependencies
# ================================================================
echo "[...] Creating Python virtual environment..."
if [ ! -d "venv" ]; then
    python3.11 -m venv venv
fi
source venv/bin/activate
echo "[✓] Virtual environment active ($(python3 --version))"

echo "[...] Installing Python dependencies..."
pip install -q --upgrade pip setuptools wheel
pip install -q -r requirements.txt
echo "[✓] Python dependencies installed"

python3 -c "import pandas, hmmlearn, alpaca, sklearn, fastapi, sqlalchemy; print('Key packages OK')" || {
    echo "[ERROR] Package import check failed"; exit 1
}

# ================================================================
# 4. Directories
# ================================================================
mkdir -p logs logs/jobs state runs/governance data_cache
echo "[✓] Directories ready"

# ================================================================
# 5. Build Next.js frontend
# ================================================================
echo "[...] Building Next.js frontend (this takes ~2 min)..."
cd "$REPO_DIR/web-app"

# production env — no NEXT_PUBLIC_API_URL means relative /api/* calls via nginx
cat > .env.local <<'ENVEOF'
# Production: API calls go to /api/* which nginx proxies to FastAPI :8000
NEXT_PUBLIC_API_URL=
ENVEOF

npm ci --prefer-offline --quiet 2>&1 | tail -3
npm run build 2>&1 | tail -10
echo "[✓] Next.js frontend built"
cd "$REPO_DIR"

# ================================================================
# 6. nginx configuration
# ================================================================
echo "[...] Configuring nginx..."

# stop the old port-80 FastAPI service if it's still claiming port 80
if sudo systemctl is-active --quiet regime-dashboard 2>/dev/null; then
    sudo systemctl stop regime-dashboard 2>/dev/null || true
fi

sudo cp scripts/nginx-regime-terminal.conf /etc/nginx/conf.d/regime-terminal.conf

# remove the default nginx config to avoid port-80 conflict
sudo rm -f /etc/nginx/conf.d/default.conf /etc/nginx/sites-enabled/default 2>/dev/null || true

sudo nginx -t
sudo systemctl enable --now nginx
sudo systemctl reload nginx
echo "[✓] nginx configured (port 80 → :8000 API / :3000 UI)"

# ================================================================
# 7. FastAPI service (port 8000)
# ================================================================
echo "[...] Installing regime-dashboard (FastAPI, port 8000)..."
sudo cp scripts/regime-dashboard.service /etc/systemd/system/regime-dashboard.service
sudo systemctl daemon-reload
sudo systemctl enable --now regime-dashboard
sleep 2
sudo systemctl is-active --quiet regime-dashboard && echo "[✓] regime-dashboard running on :8000" || {
    echo "[WARN] regime-dashboard not running — check: sudo journalctl -u regime-dashboard -n 30"
}

# ================================================================
# 8. Next.js service (port 3000)
# ================================================================
echo "[...] Installing regime-webapp (Next.js, port 3000)..."
sudo cp scripts/regime-webapp.service /etc/systemd/system/regime-webapp.service
sudo systemctl daemon-reload
sudo systemctl enable --now regime-webapp
sleep 2
sudo systemctl is-active --quiet regime-webapp && echo "[✓] regime-webapp running on :3000" || {
    echo "[WARN] regime-webapp not running — check: sudo journalctl -u regime-webapp -n 30"
}

# ================================================================
# 8b. Omnivore live paper-trading loop (installed, NOT auto-started)
# ================================================================
echo "[...] Installing regime-omnivore.service (live paper-trading loop)..."
sudo cp scripts/regime-omnivore.service /etc/systemd/system/regime-omnivore.service
sudo systemctl daemon-reload
# Placing live (paper) orders on a schedule requires an explicit operator
# go-ahead, so this unit is installed but NOT auto-enabled. If it has already
# been turned on, a redeploy restarts it so it picks up the new code.
if systemctl is-enabled --quiet regime-omnivore 2>/dev/null; then
    sudo systemctl restart regime-omnivore
    echo "[✓] regime-omnivore enabled — restarted with new code"
else
    echo "[i] regime-omnivore installed (stopped). Start with: sudo systemctl enable --now regime-omnivore"
fi

# ================================================================
# 8c. Gemini agent supervisor (installed, NOT auto-started)
# ================================================================
echo "[...] Installing regime-agents.service (always-on agent supervisor)..."
sudo cp scripts/regime-agents.service /etc/systemd/system/regime-agents.service
sudo systemctl daemon-reload
# Same convention as omnivore: the first start is an explicit operator step
# (needs GOOGLE_API_KEY in .env); afterwards redeploys restart it.
if systemctl is-enabled --quiet regime-agents 2>/dev/null; then
    sudo systemctl restart regime-agents
    echo "[✓] regime-agents enabled — restarted with new code"
else
    echo "[i] regime-agents installed (stopped). Start with: sudo systemctl enable --now regime-agents"
fi

# ================================================================
# 9. Cron (daily governance)
# ================================================================
echo "[...] Setting up cron job for daily governance..."
CRON_CMD="0 19 * * * bash $REPO_DIR/scripts/run_governance_ec2.sh"
if crontab -l 2>/dev/null | grep -qF "run_governance_ec2.sh"; then
    # update to current path/time in case it changed
    (crontab -l 2>/dev/null | grep -vF "run_governance_ec2.sh" || true; echo "$CRON_CMD") | crontab -
    echo "[✓] Cron job updated (19:00 UTC)"
else
    (crontab -l 2>/dev/null || true; echo "$CRON_CMD") | crontab -
    echo "[✓] Cron job installed (19:00 UTC)"
fi

# ================================================================
# 10. Log rotation
# ================================================================
echo "[...] Configuring log rotation..."
sudo tee /etc/logrotate.d/regime-trader > /dev/null <<EOF
$REPO_DIR/logs/*.log {
    daily
    rotate 30
    compress
    delaycompress
    notifempty
    create 0640 $(id -un) $(id -gn)
    missingok
}
EOF
echo "[✓] Log rotation configured"

# ================================================================
# Summary
# ================================================================
echo ""
echo "======================================================================="
echo "[✓] EC2 Setup Complete!"
echo "======================================================================="
echo ""
echo "Services:"
printf "  %-28s %s\n" "nginx (port 80, public)"   "$(sudo systemctl is-active nginx)"
printf "  %-28s %s\n" "regime-dashboard (:8000)"  "$(sudo systemctl is-active regime-dashboard)"
printf "  %-28s %s\n" "regime-webapp (:3000)"      "$(sudo systemctl is-active regime-webapp)"
printf "  %-28s %s\n" "redis6"                     "$(sudo systemctl is-active redis6)"
echo ""
echo "URLs (once .env is present and services are up):"
echo "  • Regime Terminal UI  → http://$(curl -s http://169.254.169.254/latest/meta-data/public-ipv4 2>/dev/null || echo '<EC2-IP>')/"
echo "  • API health check    → http://$(curl -s http://169.254.169.254/latest/meta-data/public-ipv4 2>/dev/null || echo '<EC2-IP>')/health"
echo ""
echo "If .env is not yet on EC2, copy it now:"
echo "   scp -i ec2/trad_bot.pem .env ec2-user@13.211.89.125:$REPO_DIR/"
echo "   ssh ... 'sudo systemctl restart regime-dashboard regime-webapp'"
echo ""
echo "Logs:"
echo "   sudo journalctl -u regime-dashboard -u regime-webapp -u nginx -f"
echo ""
