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
  echo "Restarting monitor and dashboard via systemd..."
  ssh -i "$OCI_KEY" "$OCI_HOST" "
    sudo systemctl restart diamond-monitor
    sudo systemctl restart diamond-dashboard
    sleep 3
    echo '--- Monitor status ---'
    systemctl is-active diamond-monitor
    echo '--- Dashboard status ---'
    systemctl is-active diamond-dashboard
    echo '--- Monitor log ---'
    journalctl -u diamond-monitor --no-pager -n 5
  "
else
  echo "Files deployed. Run with --restart to also restart services."
fi
