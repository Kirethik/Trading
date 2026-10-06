#!/bin/bash
# EC2 Deployment Script — run from your local machine
# Packages the codebase (tar+scp — no rsync dependency) and triggers remote setup
# Target: Amazon Linux 2023, user ec2-user

set -e

# ================================================================
# Configuration
# ================================================================
EC2_IP="${1:-13.211.89.125}"
EC2_USER="ec2-user"
PEM_FILE="ec2/trad_bot.pem"
REMOTE_DIR="/home/ec2-user/regime-trader"
ARCHIVE="/tmp/regime-trader-deploy.tar.gz"
# ssh needs -T (no pty) and -n (no stdin); scp accepts neither
SSH_OPTS="-T -n -o BatchMode=yes -o GSSAPIAuthentication=no -o PreferredAuthentications=publickey -i $PEM_FILE -o StrictHostKeyChecking=no -o ConnectTimeout=10 -o ServerAliveInterval=20 -o ServerAliveCountMax=6"
SCP_OPTS="-o BatchMode=yes -o GSSAPIAuthentication=no -o PreferredAuthentications=publickey -i $PEM_FILE -o StrictHostKeyChecking=no -o ConnectTimeout=10 -o ServerAliveInterval=20 -o ServerAliveCountMax=6"

# ================================================================
# Pre-flight checks
# ================================================================
if [ ! -f "$PEM_FILE" ]; then
    echo "[ERROR] PEM file not found at $PEM_FILE"
    exit 1
fi

chmod 600 "$PEM_FILE"
echo "[✓] PEM file permissions set to 600"

echo "[...] Testing SSH connectivity to $EC2_IP..."
ssh $SSH_OPTS "$EC2_USER@$EC2_IP" "echo 'SSH connection OK'" || {
    echo "[ERROR] Cannot connect to EC2. Check IP and PEM file."
    exit 1
}
echo "[✓] SSH connection verified"

# ================================================================
# Package code
# ================================================================
echo "[...] Packaging codebase (Python + Next.js source)..."
tar czf "$ARCHIVE" \
    --exclude='.git' \
    --exclude='__pycache__' \
    --exclude='.pytest_cache' \
    --exclude='data_cache' \
    --exclude='logs' \
    --exclude='state' \
    --exclude='runs' \
    --exclude='ec2' \
    --exclude='.env' \
    --exclude='venv' \
    --exclude='.venv' \
    --exclude='Api key and others.txt' \
    --exclude='.ecc_catalog_tmp.json' \
    --exclude='.ecc_skills_desc.txt' \
    --exclude='node_modules' \
    --exclude='.next' \
    --exclude='web-app/.env.local' \
    .
echo "[✓] Archive created ($(du -h "$ARCHIVE" | cut -f1))"

# ================================================================
# Deploy code
# ================================================================
echo "[...] Transferring archive to EC2..."
scp $SCP_OPTS "$ARCHIVE" "$EC2_USER@$EC2_IP:/tmp/"
ssh $SSH_OPTS "$EC2_USER@$EC2_IP" \
    "mkdir -p $REMOTE_DIR && tar xzf /tmp/regime-trader-deploy.tar.gz -C $REMOTE_DIR && rm -f /tmp/regime-trader-deploy.tar.gz"
rm -f "$ARCHIVE"
echo "[✓] Code synced to EC2"

# ================================================================
# Run setup on EC2
# ================================================================
echo "[...] Running setup on EC2 (~3-5 min for first run)..."
ssh $SSH_OPTS "$EC2_USER@$EC2_IP" "cd $REMOTE_DIR && bash scripts/ec2_setup.sh"

echo ""
echo "======================================================================="
echo "[✓] EC2 Deployment Complete!"
echo "======================================================================="
echo ""
echo "Services running behind nginx on port 80:"
echo "  • FastAPI API    → http://$EC2_IP/api/*   (port 8000 internal)"
echo "  • Next.js UI     → http://$EC2_IP/         (port 3000 internal)"
echo ""
echo "Next steps:"
echo "1. Copy .env with credentials if not already there:"
echo "   scp $SSH_OPTS .env $EC2_USER@$EC2_IP:$REMOTE_DIR/"
echo "   ssh $SSH_OPTS $EC2_USER@$EC2_IP 'sudo systemctl restart regime-dashboard regime-webapp'"
echo ""
echo "2. Check service health:"
echo "   ssh $SSH_OPTS $EC2_USER@$EC2_IP 'sudo systemctl status regime-dashboard regime-webapp nginx'"
echo ""
echo "3. Tail logs:"
echo "   ssh $SSH_OPTS $EC2_USER@$EC2_IP 'sudo journalctl -u regime-dashboard -u regime-webapp -f'"
echo ""
