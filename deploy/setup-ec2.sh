#!/bin/bash
# One-shot setup script for Amazon Linux 2023 EC2 instance.
# Run as ec2-user:  bash setup-ec2.sh
#
# Prerequisites:
#   - EC2 instance with IAM role granting bedrock:InvokeModel
#   - Security group allowing inbound TCP 80 from your team's IPs
#   - This repo cloned to ~/nj_lead_pipeline_v2

set -e

APP_DIR="$HOME/nj_lead_pipeline_v2"
cd "$APP_DIR"

echo "=== Installing system packages ==="
sudo dnf install -y python3.11 python3.11-pip git

echo "=== Creating virtual environment ==="
python3.11 -m venv .venv
source .venv/bin/activate

echo "=== Installing Python dependencies ==="
pip install --upgrade pip
pip install -r requirements.txt
pip install -e .

echo "=== Creating local directories ==="
mkdir -p input_pdfs output_csvs credentials data/reference

echo "=== Initializing database ==="
export NJLEAD_DB="$APP_DIR/leads.db"
python -c "from njlead.db.session import init_db; init_db()"

echo "=== Installing systemd service ==="
sudo cp deploy/njlead-web.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable njlead-web

echo ""
echo "=== Setup complete! ==="
echo ""
echo "Next steps:"
echo "  1. Copy your .env file to $APP_DIR/.env"
echo "  2. Copy your Google service account JSON to $APP_DIR/credentials/service-account.json"
echo "  3. Run: njlead refresh-reference"
echo "  4. Start the service: sudo systemctl start njlead-web"
echo "  5. Visit http://<your-ec2-ip> in a browser"
