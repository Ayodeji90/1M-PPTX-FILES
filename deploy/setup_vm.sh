#!/bin/bash
# PptxSweeper VM Deployment Script
# Usage: bash setup_vm.sh <FOLDER_NAME>
# Example: bash setup_vm.sh SECOND
#
# This script:
# 1. Installs system dependencies (python3, rclone, libreoffice, poppler)
# 2. Clones the repo from GitHub (requires a GitHub token)
# 3. Sets up Python venv and installs dependencies
# 4. Creates data directories
# 5. Writes the .env file with the correct Drive folder
# 6. Installs and starts the systemd service
#
# After running this script, you still need to:
# - Run 'rclone config' to set up Google Drive authentication
# - Restart the service after rclone is configured

set -euo pipefail

FOLDER_NAME="${1:?Usage: bash setup_vm.sh <FOLDER_NAME>}"
REPO_URL="https://github.com/Ayodeji90/1M-PPTX-FILES.git"
REPO_DIR="$HOME/1M-PPTX-FILES"
VENV_DIR="$REPO_DIR/.venv"
CONTACT_EMAIL="olamidesolaja90@gmail.com"

echo "============================================="
echo "  PptxSweeper VM Setup — Folder: $FOLDER_NAME"
echo "============================================="

# Step 1: System dependencies
echo ""
echo "[1/6] Installing system dependencies..."
sudo apt-get update -qq
sudo apt-get install -y -qq python3-pip python3-venv rclone libreoffice-core libreoffice-impress poppler-utils git

# Step 2: Clone repo
echo ""
echo "[2/6] Cloning repository..."
if [ -d "$REPO_DIR" ]; then
    echo "  Repo already exists at $REPO_DIR, pulling latest..."
    cd "$REPO_DIR"
    git pull origin main 2>/dev/null || echo "  git pull failed (maybe no GitHub token). Continuing..."
else
    echo "  IMPORTANT: You need a GitHub Personal Access Token."
    echo "  If this fails, set it up with:"
    echo "    git config --global credential.helper store"
    echo "    echo 'https://YOUR_TOKEN@github.com' > ~/.git-credentials"
    echo ""
    cd "$HOME"
    git clone "$REPO_URL" 2>/dev/null || {
        echo "  Clone failed. Please set up GitHub credentials and re-run this script."
        exit 1
    }
    cd "$REPO_DIR"
fi

# Step 3: Python environment
echo ""
echo "[3/6] Setting up Python environment..."
if [ ! -d "$VENV_DIR" ]; then
    python3 -m venv "$VENV_DIR"
fi
"$VENV_DIR/bin/pip" install -q --upgrade pip
"$VENV_DIR/bin/pip" install -q -r requirements.txt
"$VENV_DIR/bin/pip" install -q -e .

# Step 4: Data directories
echo ""
echo "[4/6] Creating data directories..."
mkdir -p data/staging data/pages data/logs data/registry data/review data/tmp_downloads data/status data/batch_build data/manifests

# Step 5: .env file
echo ""
echo "[5/6] Writing .env file..."
cat > "$REPO_DIR/.env" << ENVEOF
# PptxSweeper environment configuration
CONTACT_EMAIL=$CONTACT_EMAIL
RCLONE_REMOTE=gdrive
RCLONE_ROOT_FOLDER=$FOLDER_NAME
PPTXSWEEPER_OVERRIDE=
NODE_ID=0
NODE_COUNT=1
BRAVE_API_KEY=
GITHUB_TOKEN=
ENVEOF
echo "  .env configured with RCLONE_ROOT_FOLDER=$FOLDER_NAME"

# Step 6: Systemd service
echo ""
echo "[6/6] Installing systemd service..."
cat > /tmp/pptxsweeper.service << SVCEOF
[Unit]
Description=PptxSweeper million-scale .ppt/.pptx acquisition pipeline
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$(whoami)
Group=$(whoami)
WorkingDirectory=$REPO_DIR
Environment=PPTXSWEEPER_OVERRIDE=
EnvironmentFile=$REPO_DIR/.env
ExecStart=$VENV_DIR/bin/pptxsweeper run
Restart=always
RestartSec=30
KillSignal=SIGTERM
TimeoutStopSec=120
LimitNOFILE=65536
LimitNPROC=1024
MemoryHigh=12G
MemoryMax=14G
MemorySwapMax=2G
OOMPolicy=kill

[Install]
WantedBy=multi-user.target
SVCEOF

sudo cp /tmp/pptxsweeper.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable pptxsweeper

echo ""
echo "============================================="
echo "  Setup complete!"
echo "============================================="
echo ""
echo "NEXT STEPS (manual):"
echo ""
echo "  1. Set up rclone for Google Drive:"
echo "     rclone config"
echo "     - Name: gdrive"
echo "     - Type: Google Drive"
echo "     - Follow the OAuth URL to authorize"
echo ""
echo "  2. Verify rclone works:"
echo "     rclone lsd gdrive:"
echo ""
echo "  3. Start the pipeline:"
echo "     sudo systemctl start pptxsweeper"
echo ""
echo "  4. Monitor:"
echo "     systemctl status pptxsweeper"
echo "     journalctl -u pptxsweeper -f"
echo ""
