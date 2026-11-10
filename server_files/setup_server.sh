#!/bin/bash
# =============================================================
#  VoiceMap — Remote Server Setup Script
#  Run this on vpn.pussertif.id as ria.lestari
#  Usage: bash setup_server.sh
# =============================================================

set -e  # Exit immediately on any error

VOICEMAP_DIR="$HOME/voicemap"
VENV="$VOICEMAP_DIR/venv"

echo "============================================================"
echo "  VoiceMap Server Setup"
echo "  User: $(whoami)  |  Dir: $VOICEMAP_DIR"
echo "============================================================"
echo ""

# ── Step 1: Detect OS & install system packages ───────────────
echo "[1/7] Installing system packages..."
if command -v apt &>/dev/null; then
    sudo apt update -y
    sudo apt install -y python3 python3-pip python3-venv ffmpeg curl
elif command -v dnf &>/dev/null; then
    sudo dnf install -y python3 python3-pip ffmpeg curl
elif command -v yum &>/dev/null; then
    sudo yum install -y python3 python3-pip ffmpeg curl
else
    echo "WARNING: Unknown package manager. Please install python3, pip, ffmpeg manually."
fi
echo "  OK: System packages ready"
echo ""

# ── Step 2: Create voicemap directory ────────────────────────
echo "[2/7] Setting up project directory..."
mkdir -p "$VOICEMAP_DIR/reports/chunks"
echo "  OK: Directory $VOICEMAP_DIR"
echo ""

# ── Step 3: Python virtual environment ────────────────────────
echo "[3/7] Creating Python virtual environment..."
python3 -m venv "$VENV"
source "$VENV/bin/activate"
pip install --upgrade pip --quiet
pip install flask flask-cors faster-whisper requests pyTelegramBotAPI
echo "  OK: Python venv + packages installed"
echo ""

# ── Step 4: Install Ollama ────────────────────────────────────
echo "[4/7] Installing Ollama..."
if command -v ollama &>/dev/null; then
    echo "  INFO: Ollama already installed: $(ollama --version)"
else
    curl -fsSL https://ollama.com/install.sh | sh
    echo "  OK: Ollama installed"
fi

# Enable Ollama as a system service (no reboot needed)
sudo systemctl enable ollama 2>/dev/null || true
sudo systemctl start ollama 2>/dev/null || true
sleep 3
echo "  OK: Ollama service started"
echo ""

# ── Step 5: Pull Qwen models ──────────────────────────────────
echo "[5/7] Pulling Qwen model (this may take a while)..."
ollama pull qwen3:latest
echo "  OK: Qwen model ready"
echo ""

# ── Step 6: Open firewall port 5000 ──────────────────────────
echo "[6/7] Opening firewall port 5000..."
if command -v ufw &>/dev/null; then
    sudo ufw allow 5000/tcp
    sudo ufw reload
    echo "  OK: ufw port 5000 opened"
elif command -v firewall-cmd &>/dev/null; then
    sudo firewall-cmd --permanent --add-port=5000/tcp
    sudo firewall-cmd --reload
    echo "  OK: firewalld port 5000 opened"
else
    echo "  WARNING: No known firewall manager. Open port 5000 manually."
fi
echo ""

# ── Step 7: Install & start systemd services ─────────────────
echo "[7/7] Installing systemd services..."

sudo cp "$VOICEMAP_DIR/voicemap-whisper.service" /etc/systemd/system/
sudo cp "$VOICEMAP_DIR/voicemap-bot.service"     /etc/systemd/system/

sudo systemctl daemon-reload

sudo systemctl enable voicemap-whisper
sudo systemctl start  voicemap-whisper

sudo systemctl enable voicemap-bot
sudo systemctl start  voicemap-bot

echo "  OK: Services registered and started (no reboot needed)"
echo ""

# ── Done ──────────────────────────────────────────────────────
echo "============================================================"
echo "  VoiceMap setup complete!"
echo ""
echo "  Check service status:"
echo "    sudo systemctl status voicemap-whisper"
echo "    sudo systemctl status voicemap-bot"
echo ""
echo "  Test Whisper server locally on server:"
echo "    curl http://localhost:5000/health"
echo ""
echo "  View live logs:"
echo "    journalctl -u voicemap-whisper -f"
echo "    journalctl -u voicemap-bot -f"
echo "============================================================"
