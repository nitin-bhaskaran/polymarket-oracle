#!/bin/bash
# GCP VM Setup Script for Polymarket Oracle
# Run this on a fresh e2-micro Ubuntu 24.04 VM
#
# Usage:
#   gcloud compute instances create polymarket-oracle-vm \
#     --zone=europe-west2-a \
#     --machine-type=e2-micro \
#     --image-family=ubuntu-2404-lts-amd64 \
#     --image-project=ubuntu-os-cloud \
#     --boot-disk-size=20GB
#
#   gcloud compute ssh polymarket-oracle-vm
#   curl -sSL https://raw.githubusercontent.com/nitin-bhaskaran/polymarket-oracle/main/scripts/setup_gcp.sh | bash

set -e

echo "=========================================="
echo "  Polymarket Oracle — GCP Setup"
echo "=========================================="

# Update system
echo "→ Updating system packages..."
sudo apt update && sudo apt upgrade -y

# Install Python 3.11+
echo "→ Installing Python..."
sudo apt install -y python3 python3-pip python3-venv git

# Clone the repo
echo "→ Cloning repository..."
cd ~
if [ -d "polymarket-oracle" ]; then
    cd polymarket-oracle && git pull
else
    git clone https://github.com/nitin-bhaskaran/polymarket-oracle.git
    cd polymarket-oracle
fi

# Create virtual environment
echo "→ Setting up Python virtual environment..."
python3 -m venv venv
source venv/bin/activate

# Install dependencies
echo "→ Installing Python dependencies..."
pip install --upgrade pip
pip install -r requirements.txt

# Create directories
mkdir -p data logs config

# Copy example config if no config exists
if [ ! -f config/config.yaml ]; then
    cp config/config.example.yaml config/config.yaml
    echo ""
    echo "⚠️  IMPORTANT: Edit config/config.yaml with your credentials!"
    echo "   nano config/config.yaml"
    echo ""
fi

# Install systemd service
echo "→ Setting up systemd service..."
sudo cp scripts/polymarket-oracle.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable polymarket-oracle

echo ""
echo "=========================================="
echo "  Setup Complete!"
echo "=========================================="
echo ""
echo "Next steps:"
echo "  1. Edit config:  nano config/config.yaml"
echo "  2. Test dry run:  source venv/bin/activate && python -m core.main --dry-run --scan-once"
echo "  3. Start bot:     sudo systemctl start polymarket-oracle"
echo "  4. Check logs:    sudo journalctl -u polymarket-oracle -f"
echo "  5. Stop bot:      sudo systemctl stop polymarket-oracle"
echo ""
echo "To restart after config changes:"
echo "  sudo systemctl restart polymarket-oracle"
echo ""
