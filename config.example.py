# VoiceMap Configuration — EXAMPLE FILE
# 1. Copy this file:  copy config.example.py config.py
# 2. Fill in your own values in config.py
# 3. Never commit config.py (it is gitignored)

# Remote Whisper/Flask server IP and port
SERVER_URL = "http://YOUR_SERVER_IP:5000"

# Telegram Bot Token (get from @BotFather on Telegram)
BOT_TOKEN = "YOUR_TELEGRAM_BOT_TOKEN"

# Telegram Chat IDs allowed to control the bot
# Find your ID by messaging @userinfobot on Telegram
WHITELIST = {123456789}
