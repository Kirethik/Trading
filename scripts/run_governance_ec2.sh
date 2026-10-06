#!/bin/bash
# Daily Governance Cycle Runner (EC2/Linux version)
# Runs the full governance pipeline with logging
# Called by cron daily at 19:00 UTC (market hours)

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="$REPO_DIR/logs"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
LOG_FILE="$LOG_DIR/governance_$TIMESTAMP.log"

mkdir -p "$LOG_DIR"
cd "$REPO_DIR"

# Activate virtual environment
if [ -f venv/bin/activate ]; then
    source venv/bin/activate
fi

echo "=======================================================================" | tee -a "$LOG_FILE"
echo "Governance Cycle Start — $(date)" | tee -a "$LOG_FILE"
echo "=======================================================================" | tee -a "$LOG_FILE"
echo "" | tee -a "$LOG_FILE"

# Determine if deep validation (Saturday)
DAY_OF_WEEK=$(date +%A)
if [ "$DAY_OF_WEEK" = "Saturday" ]; then
    DEEP="--deep"
    echo "[•] Saturday detected — running DEEP validation (10k MC, sweeps, stress)" | tee -a "$LOG_FILE"
else
    DEEP=""
    echo "[•] Running cheap-daily validation" | tee -a "$LOG_FILE"
fi

echo "" | tee -a "$LOG_FILE"

# Run the governance cycle
python -m regime_trader govern-daily $DEEP 2>&1 | tee -a "$LOG_FILE"
EXIT_CODE=$?

echo "" | tee -a "$LOG_FILE"
echo "=======================================================================" | tee -a "$LOG_FILE"
if [ $EXIT_CODE -eq 0 ]; then
    echo "[✓] Governance Cycle Complete — $(date)" | tee -a "$LOG_FILE"
else
    echo "[✗] Governance Cycle Failed (exit code $EXIT_CODE) — $(date)" | tee -a "$LOG_FILE"
fi
echo "=======================================================================" | tee -a "$LOG_FILE"

# Pre-warm the interactive backtest's regime oracle for the default views so the
# first dashboard backtest after today's new bar doesn't pay the ~25-30s HMM
# refit (the oracle is shared across all strategies/params for a data span).
echo "" | tee -a "$LOG_FILE"
echo "[•] Pre-warming regime oracle (ETH/BTC daily)..." | tee -a "$LOG_FILE"
python -m regime_trader warm-oracle 2>&1 | tee -a "$LOG_FILE" || true

# Mirror today's reports into Notion (best-effort; never affects $EXIT_CODE —
# notion-sync itself always exits 0, this `|| true` is just extra insurance).
echo "" | tee -a "$LOG_FILE"
python -m regime_trader notion-sync --date "$(date +%Y-%m-%d)" 2>&1 | tee -a "$LOG_FILE" || true

exit $EXIT_CODE
