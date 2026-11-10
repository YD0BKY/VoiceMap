import os
import sys
import time
import queue
import threading
import requests
import psutil
import sounddevice as sd
import soundfile as sf
from datetime import datetime, timedelta

# ── Config ────────────────────────────────────────────────────────────────────
from config import SERVER_URL, BOT_TOKEN, WHITELIST as _WHITELIST
CHUNK_DURATION = 60  # seconds per recording chunk
SAMPLE_RATE = 16000  # Whisper works best at 16kHz
CHANNELS = 1         # Mono
DEVICE_NAME = "CABLE Output"  # Target virtual cable name
LOCAL_LOG = "D:\\VoiceMap\\local_log.txt"
PENDING_DIR = "D:\\VoiceMap\\pending_uploads"

# Create folders if they don't exist
os.makedirs(PENDING_DIR, exist_ok=True)

audio_queue = queue.Queue()
upload_queue = queue.Queue()

# ── Audio Recorder Thread ──────────────────────────────────────────────────────
def record_audio():
    print(f"[*] Locating audio device: '{DEVICE_NAME}'...")
    devices = sd.query_devices()
    device_idx = None
    for idx, dev in enumerate(devices):
        if DEVICE_NAME in dev['name'] and dev['max_input_channels'] > 0:
            device_idx = idx
            break
            
    if device_idx is None:
        print(f"[!] Error: Could not find audio input device named '{DEVICE_NAME}'!")
        print("[!] Available input devices:")
        for idx, dev in enumerate(devices):
            if dev['max_input_channels'] > 0:
                print(f"  [{idx}] {dev['name']}")
        sys.exit(1)
        
    print(f"[+] Found device: [{device_idx}] {devices[device_idx]['name']}")
    print(f"[+] Starting continuous recording ({CHUNK_DURATION}s chunks)...")

    def callback(indata, frames, time, status):
        if status:
            print(f"[!] Status warning: {status}", file=sys.stderr)
        audio_queue.put(indata.copy())

    # We record continuously using sounddevice InputStream
    try:
        with sd.InputStream(samplerate=SAMPLE_RATE, device=device_idx,
                            channels=CHANNELS, callback=callback):
            while True:
                # Accumulate samples for CHUNK_DURATION
                samples_to_collect = SAMPLE_RATE * CHUNK_DURATION
                chunk_data = []
                collected  = 0

                while collected < samples_to_collect:
                    try:
                        data = audio_queue.get(timeout=1.0)
                        chunk_data.append(data)
                        collected += len(data)
                    except queue.Empty:
                        continue

                # Merge accumulated blocks
                import numpy as np
                audio_chunk = np.concatenate(chunk_data, axis=0)

                # Save chunk to disk for upload
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                filename  = os.path.join(PENDING_DIR, f"chunk_{timestamp}.wav")
                sf.write(filename, audio_chunk, SAMPLE_RATE)

                # Push filename to uploader queue
                upload_queue.put(filename)


    except Exception as e:
        print(f"[!] Recording error: {e}")
        sys.exit(1)


# ── Telegram VPN Alert ────────────────────────────────────────────────────────
CHAT_IDS   = list(_WHITELIST)
TELE_API   = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

def send_tele_alert(text):
    """Send a message directly to Telegram (public internet — works without VPN)."""
    for chat_id in CHAT_IDS:
        try:
            requests.post(TELE_API, json={"chat_id": chat_id, "text": text},
                          timeout=10)
        except Exception as e:
            print(f"[!] Telegram alert failed: {e}")

# ── Uploader Thread (with auto-retry on VPN drop) ─────────────────────────────
def upload_worker():
    print("[*] Uploader thread active.")
    vpn_was_down = False   # track state so we only alert on transitions

    while True:
        filename = upload_queue.get()
        if not os.path.exists(filename):
            upload_queue.task_done()
            continue

        success  = False
        attempts = 0

        while not success:
            try:
                # Check server health first
                requests.get(f"{SERVER_URL}/health", timeout=3)

                # If server is healthy and VPN was previously down -> reconnect alert
                if vpn_was_down:
                    vpn_was_down = False
                    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    queued  = upload_queue.qsize() + 1   # +1 for current file
                    msg = (
                        f"VPN TERHUBUNG KEMBALI\n"
                        f"Waktu: {now_str}\n"
                        f"Antrian upload: {queued} chunk menunggu"
                    )
                    print(f"\n[+] VPN reconnected. Notifying Telegram...")
                    send_tele_alert(msg)

                # Upload the file
                print(f"[*] Uploading {os.path.basename(filename)} to Whisper server...")
                with open(filename, 'rb') as f:
                    r = requests.post(f"{SERVER_URL}/transcribe",
                                      files={'audio': f}, timeout=30)

                if r.status_code == 200:
                    data = r.json()
                    text = data.get("text", "").strip()
                    if text:
                        ts_str  = datetime.now().strftime("%H:%M:%S")
                        ts_full = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        log_line = f"[{ts_full}] {text}\n"
                        print(f"\n Transkripsi:\n[{ts_str}] {text}")
                        with open(LOCAL_LOG, "a", encoding="utf-8") as lf:
                            lf.write(log_line)
                    else:
                        print(f"[*] {os.path.basename(filename)} was silent/empty.")

                    os.remove(filename)
                    success = True
                else:
                    print(f"[!] Server returned {r.status_code}. Retrying in 10s...")
                    time.sleep(10)

            except requests.RequestException:
                attempts += 1
                if attempts == 1:
                    # First failure -> VPN just dropped, send alert
                    vpn_was_down = True
                    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    msg = (
                        f"VPN TERPUTUS!\n"
                        f"Waktu: {now_str}\n"
                        f"Server {SERVER_URL} tidak dapat dijangkau.\n"
                        f"Audio tetap direkam & disimpan lokal. "
                        f"Upload akan dilanjutkan otomatis saat VPN kembali."
                    )
                    print(f"\n[!] VPN DISCONNECTED -- notifying Telegram...")
                    send_tele_alert(msg)
                time.sleep(10)

        upload_queue.task_done()


# ── Discord Voice Channel Monitor ─────────────────────────────────────────────
def discord_monitor():
    """
    Background thread that monitors Discord's network connections.
    When Discord is in a voice channel it holds active UDP connections to
    Discord's voice servers (raddr is non-empty). When it leaves the channel,
    those UDP connections vanish.
    Sends a Telegram alert on disconnect and on recovery.
    """
    CHECK_INTERVAL = 30   # seconds between checks
    in_channel     = None  # None = unknown yet, True/False = known state

    print("[Discord] Voice channel monitor started.")

    def discord_has_voice_udp():
        """Return True if any Discord.exe process has an established UDP connection
        to a remote address (= actively in a voice channel)."""
        for proc in psutil.process_iter(['name']):
            try:
                if 'discord' not in proc.info['name'].lower():
                    continue
                for conn in proc.net_connections(kind='udp'):
                    # raddr is a named tuple; empty tuple means not connected
                    if conn.raddr and conn.raddr.ip:
                        return True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        return False

    def discord_running():
        return any('discord.exe' in p.name().lower()
                   for p in psutil.process_iter(['name']))

    while True:
        try:
            if not discord_running():
                if in_channel is not False:
                    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    send_tele_alert(
                        f"DISCORD TIDAK BERJALAN!\n"
                        f"Waktu: {now_str}\n"
                        f"Proses Discord.exe tidak ditemukan. "
                        f"Rekaman audio mungkin terganggu."
                    )
                    print("[Discord] Discord not running — alert sent.")
                in_channel = False
            else:
                currently_in = discord_has_voice_udp()

                if in_channel is None:
                    # First check — just record state, no alert
                    in_channel = currently_in
                    status = "in voice channel" if in_channel else "NOT in voice channel"
                    print(f"[Discord] Initial state: {status}")

                elif in_channel and not currently_in:
                    # Was in channel → left channel
                    in_channel = False
                    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    send_tele_alert(
                        f"DISCORD KELUAR DARI VOICE CHANNEL!\n"
                        f"Waktu: {now_str}\n"
                        f"Koneksi UDP ke server Discord terputus.\n"
                        f"Silakan join ulang ke voice channel."
                    )
                    print("[Discord] Left voice channel — alert sent.")

                elif not in_channel and currently_in:
                    # Was not in channel → joined channel
                    in_channel = True
                    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    send_tele_alert(
                        f"DISCORD KEMBALI DI VOICE CHANNEL\n"
                        f"Waktu: {now_str}\n"
                        f"Koneksi UDP ke server Discord aktif kembali."
                    )
                    print("[Discord] Joined voice channel — recovery alert sent.")

        except Exception as e:
            print(f"[Discord] Monitor error: {e}")

        time.sleep(CHECK_INTERVAL)


# ── Restore Local Log to Server on Startup ────────────────────────────────────
def restore_local_log():
    """
    On startup, read local_log.txt and re-push entries within the current
    operational period (16:00 yesterday -> now) to the server session.

    Uses day-rollover detection: walks log lines in order and increments
    the date whenever the clock goes backwards by >1 hour, so old HH:MM:SS
    entries are assigned correct dates without guessing.
    """
    if not os.path.exists(LOCAL_LOG):
        return

    DAILY_HOUR = 16
    now = datetime.now()
    cutoff = now.replace(hour=DAILY_HOUR, minute=0, second=0, microsecond=0)
    if cutoff > now:
        cutoff -= timedelta(days=1)

    print(f"[+] Restoring log since {cutoff.strftime('%Y-%m-%d %H:%M')}...")

    # ── Parse all lines, assigning absolute datetimes via rollover detection ──
    def parse_time_only(ts_str):
        for fmt in ("%H:%M:%S", "%I:%M:%S %p"):
            try:
                return datetime.strptime(ts_str, fmt), False
            except ValueError:
                pass
        return None, False

    all_raw = []
    try:
        with open(LOCAL_LOG, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or not line.startswith("[") or "]" not in line:
                    continue
                ts_str = line[1:line.index("]")]
                text   = line[line.index("]")+1:].strip()
                if not text:
                    continue
                # Try full datetime first
                dt, is_abs = None, False
                for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
                    try:
                        dt = datetime.strptime(ts_str, fmt)
                        is_abs = True
                        break
                    except ValueError:
                        pass
                if not is_abs:
                    dt, _ = parse_time_only(ts_str)
                if dt is not None:
                    all_raw.append((ts_str, dt, is_abs, text))
    except Exception as e:
        print(f"[!] Failed to read local log: {e}")
        return

    if not all_raw:
        print("[+] No log entries found.")
        return

    # Count rollovers to determine starting date
    rollover_count = 0
    prev_min = None
    for _, dt, _, _ in all_raw:
        cur_min = dt.hour * 60 + dt.minute
        if prev_min is not None and cur_min < prev_min - 60:
            rollover_count += 1
        prev_min = cur_min

    start_date = now.date() - timedelta(days=rollover_count)
    current_date = start_date
    prev_min = None

    assigned = []
    for ts_str, dt, is_abs, text in all_raw:
        if is_abs:
            line_dt = dt
            current_date = dt.date()
        else:
            cur_min = dt.hour * 60 + dt.minute
            if prev_min is not None and cur_min < prev_min - 60:
                current_date = current_date + timedelta(days=1)
            prev_min = cur_min
            line_dt = datetime(current_date.year, current_date.month, current_date.day,
                               dt.hour, dt.minute, dt.second)
        assigned.append((line_dt, text))

    # Filter to operational period and deduplicate
    entries = []
    seen = set()
    for line_dt, text in assigned:
        if line_dt < cutoff or line_dt > now:
            continue
        key = (line_dt.strftime("%Y-%m-%d %H:%M:%S"), text[:40])
        if key in seen:
            continue
        seen.add(key)
        entries.append({
            "text": text,
            "timestamp_ms": int(line_dt.timestamp() * 1000),
            "timestamp_str": line_dt.strftime("%H:%M:%S"),
        })

    if not entries:
        print("[+] No log entries in current operational period.")
        return

    entries.sort(key=lambda x: x["timestamp_ms"])
    print(f"[+] Found {len(entries)} entries ({assigned[0][0].strftime('%Y-%m-%d')} rollover-corrected)")

    # Clear server session then push fresh
    try:
        requests.post(f"{SERVER_URL}/session/clear", timeout=5)
        print("[+] Server session cleared.")
    except Exception:
        pass

    try:
        r = requests.post(f"{SERVER_URL}/session/sync", json=entries, timeout=30)
        if r.status_code == 200:
            added = r.json().get("added", 0)
            print(f"[+] Restored {added} entries from local log to server session.")
        else:
            print(f"[!] Server returned {r.status_code} on session sync.")
    except Exception as e:
        print(f"[!] Could not push log to server (VPN may be down): {e}")

# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Scan for existing pending files on startup (discarding anything older than 24 hours)
    now_ts = time.time()
    existing_files = sorted([os.path.join(PENDING_DIR, f) for f in os.listdir(PENDING_DIR) if f.endswith(".wav")])
    valid_files = []
    if existing_files:
        for f in existing_files:
            try:
                # Discard chunks older than 24 hours (86400s)
                if now_ts - os.path.getmtime(f) > 86400:
                    os.remove(f)
                else:
                    valid_files.append(f)
            except Exception:
                pass
                
        if valid_files:
            print(f"[+] Found {len(valid_files)} unsent recent audio chunks from last session. Queueing them for upload...")
            for f in valid_files:
                upload_queue.put(f)
            
    # Restore today's log entries to server session on startup (in case of reboot)
    restore_local_log()

    # Start threads
    t_record  = threading.Thread(target=record_audio,    daemon=True)
    t_upload  = threading.Thread(target=upload_worker,   daemon=True)
    t_discord = threading.Thread(target=discord_monitor, daemon=True)

    t_record.start()
    t_upload.start()
    t_discord.start()

    try:
        # Keep main thread alive
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[!] Stopping recorder...")
        sys.exit(0)
