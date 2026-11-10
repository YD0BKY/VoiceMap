# VoiceMap — Radio Monitoring & Transcription System

Real-time radio monitoring system using SDR (Software Defined Radio) with AI transcription, Telegram bot reporting, and web dashboard.

## Features

- **Real-time transcription** via Whisper AI — radio audio ? text, displayed on live web dashboard
- **Telegram bot** (/laporan) — generates timestamped transcription reports with AI summary
- **Auto daily report** — fires automatically at 16:00 WIB every day
- **VPN disconnect alert** — Telegram notification when server becomes unreachable
- **Discord voice channel monitor** — alert when Discord unexpectedly leaves the voice channel
- **Multi-day session restore** — survives PC/server restarts, restores session from local log

## Architecture

```
[SDR Radio] ? [Discord / VB-Cable] ? [local_recorder.py]
                                            ¦
                                      (uploads .wav chunks)
                                            ?
                                   [whisper_server.py]  ? Flask + Whisper AI
                                     (transcribes audio)
                                            ¦
                              +----------------------------+
                              ?                            ?
                       [Web Dashboard]            [telegram_bot.py]
                       (index.html + app.js)      (/laporan, auto-report)
```

## Components

| File | Runs on | Purpose |
|------|---------|---------|
| local_recorder.py | Local PC | Records audio from VB-Cable, uploads to server |
| whisper_server.py | Remote server | Whisper transcription + session API |
| 	elegram_bot.py | Remote server | Telegram bot with reporting |
| pp.js + index.html | Local PC (HTTP server) | Live web dashboard |
| start_voicemap.bat | Local PC | Launcher for recorder + web server |

## Setup

### 1. Clone & Configure

```bash
git clone https://github.com/YOUR_USERNAME/VoiceMap.git
cd VoiceMap
copy config.example.py config.py   # Windows
# cp config.example.py config.py  # Linux/Mac
```

Edit config.py with your values:
```python
SERVER_URL = "http://YOUR_SERVER_IP:5000"
BOT_TOKEN  = "YOUR_TELEGRAM_BOT_TOKEN"
WHITELIST  = {YOUR_TELEGRAM_CHAT_ID}
```

### 2. Local PC (Windows)

```bash
pip install sounddevice soundfile requests numpy psutil
```

Set DEVICE_NAME in local_recorder.py to match your VB-Cable device name.

Run:
```
start_voicemap.bat
```

### 3. Remote Server (Linux)

```bash
pip install -r requirements_server.txt
# Copy server_files/*.service to /etc/systemd/system/
sudo systemctl enable voicemap-whisper voicemap-bot
sudo systemctl start voicemap-whisper voicemap-bot
```

Requires: whisper, lask, pytelegrambotapi, ollama (for AI summaries)

## Telegram Bot Commands

| Command | Description |
|---------|-------------|
| /laporan | Generate transcript report for current period (since 16:00) |
| /summary | AI summary of current period |

## Configuration

All secrets live in config.py (gitignored). See config.example.py for the template.

| Variable | Description |
|----------|-------------|
| SERVER_URL | IP:port of the remote Whisper server |
| BOT_TOKEN | Telegram bot token from @BotFather |
| WHITELIST | Set of Telegram chat IDs authorized to use the bot |
