#!/bin/bash
# Deploy DIAMOND to OCI compute instance
# Usage: ./deploy.sh [--restart]
#   --restart  Also restart the monitor after deploying

OCI_HOST="ubuntu@129.158.40.51"
OCI_KEY="$HOME/.ssh/hmm-trader.key"
OCI_DIR="~/kalshi-diamond"
PYTHON="/home/ubuntu/miniconda3/bin/python"

echo "Deploying DIAMOND to OCI..."

# Sync Python files (excludes secrets, db, logs)
scp -i "$OCI_KEY" \
  diamond_monitor.py diamond_config.py diamond_dashboard.py \
  diamond_dashboard_lite.py diamond_backtest.py \
  "$OCI_HOST:$OCI_DIR/" 2>&1

scp -i "$OCI_KEY" \
  src/*.py \
  "$OCI_HOST:$OCI_DIR/src/" 2>&1

echo "Files synced."

if [ "$1" = "--restart" ]; then
  echo "Restarting monitor and dashboard..."
  ssh -i "$OCI_KEY" "$OCI_HOST" "
    pkill -f diamond_monitor; pkill -f diamond_dashboard; sleep 1
    cd $OCI_DIR
    nohup $PYTHON diamond_monitor.py > diamond_monitor.log 2>&1 &
    nohup $PYTHON diamond_dashboard.py > diamond_dashboard.log 2>&1 &
    sleep 3
    echo '--- Monitor log ---'
    tail -5 diamond_monitor.log
    echo '--- Processes ---'
    ps aux | grep diamond | grep -v grep
  "
else
  echo "Files deployed. Run with --restart to also restart services."
fi
