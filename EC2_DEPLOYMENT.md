# EC2 Deployment Guide — Regime-Trader Migration

## Overview

The Regime-Trader system has been migrated from Windows to EC2 (Amazon Linux
2023) to run continuous governance cycles on a dedicated cloud instance. This
guide walks through the deployment process.

## Infrastructure Details

- **EC2 Instance**: Amazon Linux 2023 (AL2023, dnf-based)
- **IP Address**: 13.211.89.125 (stored in `ec2/New Text Document.txt`)
- **SSH Key**: `ec2/trad_bot.pem` (private key — never commit)
- **User**: `ec2-user` (default Amazon Linux AMI user)
- **Remote Directory**: `/home/ec2-user/regime-trader`
- **Python**: 3.11 (installed via `dnf install python3.11 python3.11-pip`;
  the system `python3` is 3.9 and has no pip)
- **Cron**: provided by the `cronie` package, service name `crond`

## Pre-Deployment Checklist

On your **local machine**:

1. ✅ SSH key exists at `ec2/trad_bot.pem`
2. ✅ EC2 IP stored in `ec2/New Text Document.txt`
3. ✅ All source code committed to git
4. ✅ `.env` file prepared with credentials (not in repo):
   ```
   ALPACA_API_KEY=<your-paper-key>
   ALPACA_SECRET_KEY=<your-paper-secret>
   GOOGLE_API_KEY=<gemini-key-billing-enabled>   # agents; optional (deterministic without)
   EXA_API_KEY=<optional>
   FIRECRAWL_API_KEY=<optional>
   ```

## One-Command Deployment

From your local machine, in the project root:

```bash
bash scripts/ec2_deploy.sh [optional-ip-address]
```

This script:
1. Validates SSH connectivity
2. Packages the codebase with `tar` (excluding `.git`, cache, logs, `state`,
   `runs`, `ec2/`, `.env`, venvs) and ships it via `scp` (no `rsync` dependency)
3. Extracts the archive into `/home/ec2-user/regime-trader` on the instance
4. Runs the remote setup script (`scripts/ec2_setup.sh`), which installs
   Python 3.11 + dependencies and configures cron

**Example:**
```bash
bash scripts/ec2_deploy.sh 13.211.89.125
```

## Manual Deployment (Step-by-Step)

If you prefer to deploy manually:

### 1. Set variables

```bash
EC2_IP="13.211.89.125"
EC2_USER="ec2-user"
PEM_FILE="ec2/trad_bot.pem"
REPO_DIR="/home/ec2-user/regime-trader"
```

### 2. Test SSH

```bash
ssh -i $PEM_FILE $EC2_USER@$EC2_IP "echo OK"
```

### 3. Sync code (tar + scp)

```bash
chmod 600 $PEM_FILE
tar czf /tmp/regime-trader-deploy.tar.gz \
  --exclude='.git' --exclude='__pycache__' --exclude='.pytest_cache' \
  --exclude='data_cache' --exclude='logs' --exclude='state' --exclude='runs' \
  --exclude='ec2' --exclude='.env' --exclude='venv' --exclude='.venv' \
  .
scp -i $PEM_FILE /tmp/regime-trader-deploy.tar.gz $EC2_USER@$EC2_IP:/tmp/
ssh -i $PEM_FILE $EC2_USER@$EC2_IP \
  "mkdir -p $REPO_DIR && tar xzf /tmp/regime-trader-deploy.tar.gz -C $REPO_DIR && rm -f /tmp/regime-trader-deploy.tar.gz"
```

### 4. Run setup on EC2

```bash
ssh -i $PEM_FILE $EC2_USER@$EC2_IP "cd $REPO_DIR && bash scripts/ec2_setup.sh"
```

This installs `python3.11`, `python3.11-pip`, `git`, `cronie` via `dnf`,
creates the venv, installs `requirements.txt`, and registers the daily cron job.

### 5. Deploy .env file

```bash
scp -i $PEM_FILE .env $EC2_USER@$EC2_IP:$REPO_DIR/
```

## Post-Deployment Verification

### Test SSH access

```bash
ssh -i ec2/trad_bot.pem ec2-user@13.211.89.125
```

### Verify Python environment

```bash
cd /home/ec2-user/regime-trader
source venv/bin/activate
python -m regime_trader train
```

### Check cron job

```bash
crontab -l | grep govern
```

Expected output:
```
0 21 * * * bash /home/ec2-user/regime-trader/scripts/run_governance_ec2.sh
```

`run_governance_ec2.sh` activates the venv, detects Saturday for `--deep`
validation, and tees output to a timestamped log under `logs/`.

### Run a test governance cycle

```bash
ssh -i ec2/trad_bot.pem ec2-user@13.211.89.125
cd /home/ec2-user/regime-trader
source venv/bin/activate
python -m regime_trader govern-daily --no-trade  # Research-only, no orders
```

### Monitor logs

```bash
ssh -i ec2/trad_bot.pem ec2-user@13.211.89.125 \
  "tail -f /home/ec2-user/regime-trader/logs/governance_*.log"
```

## Daily Execution Schedule

The cron job is configured to run at:
- **Time**: 21:00 UTC (= 02:30 IST, adjusts with DST on the US side)
- **Frequency**: Daily
- **Deep validation**: Saturdays (full 10k Monte Carlo, sweeps, stress) —
  detected by `run_governance_ec2.sh` checking `date +%A`
- **Other days**: Cheap daily gates on all strategies

The cron command:
```bash
0 21 * * * bash /home/ec2-user/regime-trader/scripts/run_governance_ec2.sh
```

## Logs and Artifacts

All outputs are written to:
- **Timestamped logs**: `logs/governance_YYYYMMDD_HHMMSS.log` (one per run)
- **Reports**: `runs/governance/<YYYY-MM-DD>/` (per-strategy markdown, master
  leaderboard, decisions)

### View live logs

```bash
ssh -i ec2/trad_bot.pem ec2-user@13.211.89.125 \
  "tail -f /home/ec2-user/regime-trader/logs/governance_*.log"
```

### Download latest report

```bash
scp -r -i ec2/trad_bot.pem \
  ec2-user@13.211.89.125:/home/ec2-user/regime-trader/runs/governance/$(date +%Y-%m-%d)/ \
  ./latest_governance_report/
```

## Troubleshooting

### Cannot connect via SSH

- Check IP address in `ec2/New Text Document.txt`
- Verify PEM file at `ec2/trad_bot.pem`
- Check file permissions: `chmod 600 ec2/trad_bot.pem`
- Try: `ssh -i ec2/trad_bot.pem -v ec2-user@13.211.89.125` (verbose)

### Cron job not running

```bash
ssh -i ec2/trad_bot.pem ec2-user@13.211.89.125
crontab -l                 # Verify job exists
systemctl status crond     # cronie's service (AL2023)
journalctl -u crond --since today | grep -i govern   # AL2023 uses journald, not /var/log/syslog
```

### Python or dependency errors

```bash
ssh -i ec2/trad_bot.pem ec2-user@13.211.89.125
cd /home/ec2-user/regime-trader
source venv/bin/activate
pip list | grep -E "pandas|alpaca|hmmlearn"
python -m pytest -q
```

## Reverting to Windows Scheduler (if needed)

The original Windows Task Scheduler setup is documented in the plan file at:
- `C:\Users\mohan\.claude\plans\modifications-to-agent-jaunty-turing.md`

Commands available:
- Windows: `python -m regime_trader govern-install-schedule --register`
- EC2/Linux: Cron is auto-configured by `scripts/ec2_setup.sh`

## Updating Code on EC2

To push the latest code after making changes locally:

```bash
bash scripts/ec2_deploy.sh
```

This re-packages and re-extracts the codebase via tar+scp and re-runs
`scripts/ec2_setup.sh` (idempotent — skips steps already done, e.g. existing
venv and cron entry).

## Cost Optimization

- **EC2 instance**: 2 vCPU / ~7.6GB RAM is sufficient for daily cheap gates;
  deep (Saturday) validation is more CPU-intensive
- **Data caching**: Historical bars/oracle are cached under `data_cache/`
  (excluded from deploys); the first run on a fresh instance is slow because
  this cache is empty
- **Governance cycle**: Full cycle (cheap daily) takes significantly longer
  on a cold cache; deep validation (10k Monte Carlo × 3 methods per strategy)
  is the most expensive tier
- **AutoScaling**: Cron jobs run at fixed times, so costs are predictable

## Security Notes

- 🔒 **Never commit `.env` or `ec2/trad_bot.pem` to git**
- 🔒 **Restrict EC2 security group**: Allow SSH (port 22) only from your IP
- 🔒 **Rotate credentials**: Update Alpaca/Anthropic keys periodically
- 🔒 **Monitor cron logs**: Watch for errors or unexpected behavior

## Next Steps

1. ✅ Run `bash scripts/ec2_deploy.sh`
2. ✅ Verify with `ssh -i ec2/trad_bot.pem ec2-user@13.211.89.125`
3. ✅ Wait for first cron cycle (next 21:00 UTC)
4. ✅ Check `runs/governance/` for reports
5. ✅ Monitor logs and adjust thresholds in `config/config.yaml` as needed

---

**Last Updated**: 2026-06-12
**EC2 Setup Script**: `scripts/ec2_setup.sh`
**Deployment Script**: `scripts/ec2_deploy.sh`
**Linux Governance Runner**: `scripts/run_governance_ec2.sh`
