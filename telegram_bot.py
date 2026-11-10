# -*- coding: utf-8 -*-
"""
VoiceMap — telegram_bot.py
Telegram bot for remote access to VoiceMap transcriptions.

Bot:     @RadSTT_bot
Server:  http://localhost:5000 (Whisper + session endpoints)

Commands:
    /start        — Welcome
    /help         — List commands
    /status       — Whisper server health
    /reports      — List saved report files on disk
    /latest       — Download latest saved transcript
    /get <file>   — Download a specific report file
    /laporan      — Generate report from yesterday 16:00 to now
                    (live session + saved files + Ollama AI summary)
"""

import os
import time
import json
import re
import io
import requests
import threading
from datetime import datetime, timedelta

import telebot

# ── Config ────────────────────────────────────────────────────────────────────

from config import BOT_TOKEN, WHITELIST, SERVER_URL as _SERVER_URL
WHISPER_URL  = "http://localhost:5000"   # bot runs on the server, so localhost
OLLAMA_URL   = "http://localhost:11434"
OLLAMA_MODEL = "qwen2.5:3b-instruct-q4_K_S"
DAILY_HOUR   = 16

# ── Bot Init ──────────────────────────────────────────────────────────────────

bot = telebot.TeleBot(BOT_TOKEN, parse_mode=None)

# ── Auth Guard ────────────────────────────────────────────────────────────────

def is_allowed(message):
    return message.from_user.id in WHITELIST

def require_auth(func):
    def wrapper(message):
        if not is_allowed(message):
            print(f"[Bot] Blocked user {message.from_user.id}")
            return
        func(message)
    return wrapper

# ── Helpers ───────────────────────────────────────────────────────────────────

def get_report_range():
    now = datetime.now()
    frm = now.replace(hour=DAILY_HOUR, minute=0, second=0, microsecond=0)
    if frm > now:
        frm -= timedelta(days=1)
    return frm, now

def dt_to_ms(dt):
    return int(dt.timestamp() * 1000)

def fmt_dt(dt):
    return dt.strftime("%Y-%m-%d %H:%M")

def send_as_file(chat_id, content, filename, caption=""):
    data = io.BytesIO(content.encode("utf-8"))
    data.name = filename
    bot.send_document(chat_id, data, caption=caption)

# ── /start ────────────────────────────────────────────────────────────────────

@bot.message_handler(commands=["start"])
@require_auth
def cmd_start(message):
    bot.reply_to(message,
        "Halo! Saya VoiceMap Radio Bot.\n\n"
        "Saya bisa membantu kamu mengakses transkripsi radio dari VoiceMap.\n\n"
        "Ketik /help untuk melihat perintah yang tersedia."
    )

# ── /help ─────────────────────────────────────────────────────────────────────

@bot.message_handler(commands=["help"])
@require_auth
def cmd_help(message):
    bot.reply_to(message,
        "Perintah VoiceMap Bot:\n\n"
        "/status   - Cek status server Whisper\n"
        "/reports  - Daftar laporan tersimpan di server\n"
        "/latest   - Download transcript terbaru\n"
        "/get <nama_file> - Download file laporan tertentu\n"
        "/laporan  - Generate laporan kemarin 16:00 sampai sekarang\n"
        "           (transcript + AI summary via Ollama)\n"
        "/reset    - Bersihkan buffer session di server\n\n"
        "Semua perintah hanya untuk pengguna yang diizinkan."
    )

# ── /reset ────────────────────────────────────────────────────────────────────

@bot.message_handler(commands=["reset"])
@require_auth
def cmd_reset(message):
    try:
        r = requests.post(f"{WHISPER_URL}/session/clear", timeout=5)
        if r.status_code == 200:
            removed = r.json().get("removed", 0)
            bot.reply_to(message, f"✅ Buffer session di server telah dibersihkan ({removed} entri dihapus).")
        else:
            bot.reply_to(message, f"⚠️ Gagal reset session (HTTP {r.status_code})")
    except Exception as e:
        bot.reply_to(message, f"❌ Error: {e}")

# ── /status ────────────────────────────────────────────────────────# Helper to format date label nicely in buttons (e.g. 2026-06-28 -> 28 Jun)
def format_button_date(label):
    try:
        # Check if label has format YYYY-MM-DD
        parts = label.split("_")[0].split("-")
        if len(parts) == 3:
            day = parts[2]
            months = ["Jan", "Feb", "Mar", "Apr", "Mei", "Jun", "Jul", "Agt", "Sep", "Okt", "Nov", "Des"]
            month_idx = int(parts[1]) - 1
            if 0 <= month_idx < 12:
                return f"{day} {months[month_idx]}"
    except Exception:
        pass
    return label[:10]  # fallback to first 10 chars

def send_reports_page(chat_id, page_index=0, edit_message_id=None):
    try:
        r = requests.get(f"{WHISPER_URL}/reports", timeout=5)
        reports = r.json()
        if not reports:
            if edit_message_id:
                bot.edit_message_text("📂 Belum ada laporan tersimpan di server.", chat_id, edit_message_id)
            else:
                bot.send_message(chat_id, "📂 Belum ada laporan tersimpan di server.")
            return

        # Pagination calculations (5 items per page)
        per_page = 5
        total_pages = (len(reports) + per_page - 1) // per_page
        if page_index < 0:
            page_index = 0
        if page_index >= total_pages:
            page_index = total_pages - 1

        start_idx = page_index * per_page
        end_idx = start_idx + per_page
        page_reports = reports[start_idx:end_idx]

        msg_text = (
            f"📁 *Daftar Laporan di Server (Hal {page_index + 1}/{total_pages}):*\n\n"
            f"Klik tombol di bawah untuk langsung mengunduh file laporan.\n"
        )
        
        # Build list preview in message body
        for idx, rep in enumerate(page_reports, start=start_idx+1):
            label = rep.get("label", "?")
            lines_count = rep.get("lines", 0)
            msg_text += f"{idx}. 📅 *{label}* ({lines_count} baris)\n"

        # Build inline keyboard markup
        markup = telebot.types.InlineKeyboardMarkup()
        
        # Add a row of buttons for each report
        for rep in page_reports:
            label = rep.get("label", "?")
            btn_lbl = format_button_date(label)
            row = []
            
            tf = rep.get("transcript")
            if tf:
                row.append(telebot.types.InlineKeyboardButton(f"📄 Trn ({btn_lbl})", callback_data=f"dl:{tf}"))
            sf = rep.get("summary")
            if sf:
                row.append(telebot.types.InlineKeyboardButton(f"🤖 Sum ({btn_lbl})", callback_data=f"dl:{sf}"))
                
            if row:
                markup.row(*row)

        # Add navigation buttons
        nav_row = []
        if page_index > 0:
            nav_row.append(telebot.types.InlineKeyboardButton("⬅️ Newer", callback_data=f"page:{page_index - 1}"))
        if page_index < total_pages - 1:
            nav_row.append(telebot.types.InlineKeyboardButton("Older ➡️", callback_data=f"page:{page_index + 1}"))
            
        if nav_row:
            markup.row(*nav_row)

        if edit_message_id:
            bot.edit_message_text(msg_text, chat_id, edit_message_id, reply_markup=markup, parse_mode="Markdown")
        else:
            bot.send_message(chat_id, msg_text, reply_markup=markup, parse_mode="Markdown")

    except Exception as e:
        if edit_message_id:
            bot.edit_message_text(f"❌ Error: {e}", chat_id, edit_message_id)
        else:
            bot.send_message(chat_id, f"❌ Error: {e}")

@bot.message_handler(commands=["reports"])
@require_auth
def cmd_reports(message):
    bot.send_chat_action(message.chat.id, "typing")
    send_reports_page(message.chat.id, page_index=0)

# ── Callback Query Handler for Inline Buttons ─────────────────────────────────

@bot.callback_query_handler(func=lambda call: True)
def handle_callbacks(call):
    if not is_allowed(call):
        bot.answer_callback_query(call.id, "❌ Akses ditolak.")
        return

    data = call.data
    
    if data.startswith("page:"):
        page_idx = int(data.split(":")[1])
        bot.answer_callback_query(call.id, "Memuat halaman...")
        send_reports_page(call.message.chat.id, page_index=page_idx, edit_message_id=call.message.message_id)
        
    elif data.startswith("dl:"):
        filename = data.split(":", 1)[1]
        bot.answer_callback_query(call.id, "Mengirim file...")
        bot.send_chat_action(call.message.chat.id, "upload_document")
        try:
            r = requests.get(f"{WHISPER_URL}/reports/{filename}", timeout=10)
            if r.status_code == 404:
                bot.send_message(call.message.chat.id, f"❌ File `{filename}` tidak ditemukan di server.", parse_mode="Markdown")
                return
            send_as_file(call.message.chat.id, r.text, filename, caption=f"📄 {filename}")
        except Exception as e:
            bot.send_message(call.message.chat.id, f"❌ Gagal mengirim file: `{e}`", parse_mode="Markdown")

# ── /latest ───────────────────────────────────────────────────────────────────

@bot.message_handler(commands=["latest"])
@require_auth
def cmd_latest(message):
    bot.send_chat_action(message.chat.id, "typing")
    try:
        r = requests.get(f"{WHISPER_URL}/reports", timeout=5)
        reports = r.json()
        if not reports:
            bot.reply_to(message, "Belum ada laporan tersimpan di server.")
            return
        for rep in reports:
            fname = rep.get("transcript")
            if fname:
                file_r = requests.get(f"{WHISPER_URL}/reports/{fname}", timeout=10)
                send_as_file(message.chat.id, file_r.text, fname,
                             caption=f"Transcript terbaru: {rep['label']}")
                return
        bot.reply_to(message, "Tidak ada transcript tersimpan.")
    except Exception as e:
        bot.reply_to(message, f"Error: {e}")

# ── /get <filename> ───────────────────────────────────────────────────────────

@bot.message_handler(commands=["get"])
@require_auth
def cmd_get(message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(message, "Gunakan: /get <nama_file>\nContoh: /get transcript_2026-06-26.txt")
        return
    filename = parts[1].strip()
    bot.send_chat_action(message.chat.id, "upload_document")
    try:
        r = requests.get(f"{WHISPER_URL}/reports/{filename}", timeout=10)
        if r.status_code == 404:
            bot.reply_to(message, f"File tidak ditemukan: {filename}")
            return
        send_as_file(message.chat.id, r.text, filename, caption=filename)
    except Exception as e:
        bot.reply_to(message, f"Error: {e}")

# ── /laporan ──────────────────────────────────────────────────────────────────

def summary_worker(chat_id, from_dt, to_dt, period_label, merged, transcript_text, full_transcript, label_safe, fname_t):
    try:
        bot.send_message(chat_id, "🤖 Menghubungi Server untuk kompilasi Ringkasan AI...\n(Slot 2-jam yang sudah lewat akan diambil dari cache, slot baru akan digenerate live. Mohon tunggu...)")
        
        stop_event = threading.Event()
        def status_ticker():
            while not stop_event.is_set():
                time.sleep(15)
                if stop_event.is_set():
                    break
                try:
                    bot.send_chat_action(chat_id, "typing")
                except Exception:
                    pass

        ticker_thread = threading.Thread(target=status_ticker, daemon=True)
        ticker_thread.start()

        from_ms = int(from_dt.timestamp() * 1000)
        to_ms = int(to_dt.timestamp() * 1000)

        try:
            # We call the new server compilation endpoint (centralized logic)
            # Timeout is 600s because loading the large model on CPU could take a bit
            r = requests.post(
                f"{WHISPER_URL}/reports/compile-summary",
                json={
                    "from_ms": from_ms,
                    "to_ms": to_ms,
                    "label": label_safe
                },
                timeout=600
            )
            stop_event.set()

            if r.status_code != 200:
                bot.send_message(chat_id, f"⚠️ Gagal kompilasi summary (Server HTTP {r.status_code}). Transcript sudah dikirim.")
                return

            res_data = r.json()
            summary_text = res_data.get("summary", "").strip()

            if not summary_text:
                summary_text = "(Tidak ada respons summary dari server)"

            # Save the final compiled report to server main reports folder
            try:
                requests.post(f"{WHISPER_URL}/save-report", json={
                    "label":      from_dt.strftime("%Y-%m-%d_%H%M") + "_to_" + to_dt.strftime("%H%M"),
                    "transcript": full_transcript,
                    "summary":    summary_text,
                }, timeout=10)
            except Exception:
                pass

            fname_s = f"summary_{label_safe}.txt"
            send_as_file(chat_id, summary_text, fname_s, caption="Compiled AI Summary")

            # Inline preview of the executive summary portion
            exec_preview = res_data.get("executive", "").strip()
            if not exec_preview or "Tidak ada aktivitas" in exec_preview:
                exec_preview = summary_text[:3000]
            else:
                exec_preview = f"📝 *AI EXECUTIVE SUMMARY:*\n\n{exec_preview}"
                
            if len(exec_preview) > 3000:
                exec_preview = exec_preview[:3000] + "\n\n(Ringkasan lengkap ada di file summary di atas)"

            try:
                bot.send_message(chat_id, exec_preview, parse_mode="Markdown")
                bot.send_message(chat_id,
                    f"✅ Laporan selesai!\n"
                    f"Periode: {period_label}\n"
                    f"Entri: {len(merged)}\n"
                    f"Disimpan di server."
                )
            except Exception:
                time.sleep(2)
                bot.send_message(chat_id, exec_preview, parse_mode="Markdown")
                bot.send_message(chat_id, f"✅ Laporan selesai! Periode: {period_label}")

        except Exception as e:
            stop_event.set()
            bot.send_message(chat_id, f"⚠️ Gagal generate AI summary: {e}\nTranscript sudah dikirim.")

    except Exception as e:
        print(f"[Bot] Thread execution error: {e}")


@bot.message_handler(commands=["laporan"])
@require_auth
def cmd_laporan(message):
    from_dt, to_dt = get_report_range()
    from_ms, to_ms = dt_to_ms(from_dt), dt_to_ms(to_dt)
    period_label   = f"{fmt_dt(from_dt)} s/d {fmt_dt(to_dt)}"

    bot.reply_to(message,
        f"Membuat laporan...\n"
        f"Periode: {period_label}\n\n"
        f"Mengumpulkan data dari server..."
    )
    bot.send_chat_action(message.chat.id, "typing")

    # Step 1: Live session buffer (Single source of truth)
    live_entries = []
    try:
        r = requests.get(f"{WHISPER_URL}/session",
                         params={"from_ms": from_ms, "to_ms": to_ms}, timeout=5)
        live_entries = r.json()
        print(f"[Bot] Live entries: {len(live_entries)}")
    except Exception as e:
        print(f"[Bot] Session error: {e}")

    # Deduplicate and sort strictly by timestamp_ms
    seen_keys = set()
    merged = []
    live_sorted = sorted(
        [e for e in live_entries if e.get("timestamp_ms", 0) > 0],
        key=lambda x: x["timestamp_ms"]
    )
    for e in live_sorted:
        key = (e.get("timestamp_ms"), e.get("text", "").strip())
        if key[1] and key not in seen_keys:
            seen_keys.add(key)
            merged.append(e)

    if not merged:
        bot.send_message(message.chat.id,
            f"Tidak ada transkripsi ditemukan untuk periode:\n{period_label}"
        )
        return

    # Step 4: Build transcript
    header = (
        f"VoiceMap - Transkripsi Radio\n"
        f"Periode: {period_label}\n"
        f"Dibuat: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        + "=" * 60 + "\n\n"
    )
    body_lines = []
    for e in merged:
        ts  = e.get("timestamp_str", "")
        txt = e.get("text", "").strip()
        if txt:
            body_lines.append(f"[{ts}] {txt}" if ts else txt)

    transcript_text = "\n".join(body_lines)
    full_transcript  = header + transcript_text

    label_safe = from_dt.strftime("%Y%m%d_%H%M") + "_" + to_dt.strftime("%H%M")
    fname_t    = f"transcript_{label_safe}.txt"
    
    bot.send_chat_action(message.chat.id, "upload_document")
    try:
        send_as_file(message.chat.id, full_transcript, fname_t,
                     caption=f"Transcript ({len(body_lines)} entri)")
    except Exception:
        time.sleep(1)
        send_as_file(message.chat.id, full_transcript, fname_t,
                     caption=f"Transcript ({len(body_lines)} entri)")

    # Delegate the long Ollama summary call to a background thread
    t = threading.Thread(
        target=summary_worker,
        args=(message.chat.id, from_dt, to_dt, period_label, merged, transcript_text, full_transcript, label_safe, fname_t),
        daemon=True
    )
    t.start()


# ── Auto-Scheduler: Kirim /laporan otomatis jam 16:00 ────────────────────────

def auto_laporan_worker():
    """
    Background thread that fires the daily /laporan at 16:00 WIB.
    Catch-up: if started within 3h after 16:00 and today's report not yet sent,
    fires immediately instead of waiting until tomorrow.
    """
    REPORT_HOUR   = 16
    REPORT_MINUTE = 0
    CATCHUP_HOURS = 3    # fire immediately if within this many hours past 16:00
    last_report_date = None

    print("[Scheduler] Auto-laporan scheduler started.")
    time.sleep(5)  # let bot initialize

    while True:
        now    = datetime.now()
        target = now.replace(hour=REPORT_HOUR, minute=REPORT_MINUTE, second=0, microsecond=0)

        if now >= target:
            minutes_past = (now - target).total_seconds() / 60
            if minutes_past <= CATCHUP_HOURS * 60 and last_report_date != now.date():
                # Missed 16:00 but within catch-up window — fire now
                print(f"[Scheduler] Catch-up: missed 16:00 by {int(minutes_past)}min, firing now.")
                wait_seconds = 0
            else:
                # Already reported today or past catch-up window — wait until tomorrow
                target += timedelta(days=1)
                wait_seconds = (target - now).total_seconds()
        else:
            wait_seconds = (target - now).total_seconds()

        if wait_seconds > 0:
            print(f"[Scheduler] Next auto-laporan in {int(wait_seconds // 3600)}h "
                  f"{int((wait_seconds % 3600) // 60)}m "
                  f"(at {target.strftime('%Y-%m-%d %H:%M')})")
            time.sleep(wait_seconds)

        # Fire: send /laporan to all whitelisted users
        print("[Scheduler] Firing auto-laporan...")
        last_report_date = datetime.now().date()   # mark today as reported
        for chat_id in list(WHITELIST):
            try:
                # Laporan otomatis: periode yang BARU SELESAI = kemarin 16:00 → hari ini 16:00
                now_fire = datetime.now()
                to_dt   = now_fire.replace(hour=REPORT_HOUR, minute=0, second=0, microsecond=0)
                from_dt = to_dt - timedelta(days=1)
                from_ms, to_ms = dt_to_ms(from_dt), dt_to_ms(to_dt)
                period_label   = f"{fmt_dt(from_dt)} s/d {fmt_dt(to_dt)}"

                bot.send_message(chat_id,
                    f"🕓 *Laporan Otomatis Jam 16:00*\n"
                    f"Periode: {period_label}\n\n"
                    f"Sedang mengumpulkan data...",
                    parse_mode="Markdown"
                )

                # Fetch live session entries
                live_entries = []
                try:
                    r = requests.get(f"{WHISPER_URL}/session",
                                     params={"from_ms": from_ms, "to_ms": to_ms}, timeout=5)
                    live_entries = r.json()
                except Exception as e:
                    print(f"[Scheduler] Session error: {e}")

                # Deduplicate and sort
                seen_keys = set()
                merged = []
                for e in sorted([x for x in live_entries if x.get("timestamp_ms", 0) > 0],
                                 key=lambda x: x["timestamp_ms"]):
                    key = (e.get("timestamp_ms"), e.get("text", "").strip())
                    if key[1] and key not in seen_keys:
                        seen_keys.add(key)
                        merged.append(e)

                if not merged:
                    bot.send_message(chat_id, f"⚠️ Tidak ada transkripsi untuk periode {period_label}")
                    continue

                # Build transcript
                header = (
                    f"VoiceMap - Transkripsi Radio (Auto)\n"
                    f"Periode: {period_label}\n"
                    f"Dibuat: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                    + "=" * 60 + "\n\n"
                )
                body_lines = []
                for e in merged:
                    ts  = e.get("timestamp_str", "")
                    txt = e.get("text", "").strip()
                    if txt:
                        body_lines.append(f"[{ts}] {txt}" if ts else txt)

                transcript_text = "\n".join(body_lines)
                full_transcript = header + transcript_text
                label_safe = from_dt.strftime("%Y%m%d_%H%M") + "_" + to_dt.strftime("%H%M")
                fname_t    = f"transcript_{label_safe}.txt"

                bot.send_chat_action(chat_id, "upload_document")
                send_as_file(chat_id, full_transcript, fname_t,
                             caption=f"Transcript otomatis ({len(body_lines)} entri)")

                # AI summary in background
                t = threading.Thread(
                    target=summary_worker,
                    args=(chat_id, from_dt, to_dt, period_label, merged,
                          transcript_text, full_transcript, label_safe, fname_t),
                    daemon=True
                )
                t.start()

            except Exception as e:
                print(f"[Scheduler] Error sending auto-laporan to {chat_id}: {e}")
                try:
                    bot.send_message(chat_id, f"❌ Gagal auto-laporan: {e}")
                except Exception:
                    pass

        # Small sleep to avoid re-firing immediately if we wake up at exactly 16:00
        time.sleep(70)


# ── Unknown ───────────────────────────────────────────────────────────────────

@bot.message_handler(func=lambda m: True)
def handle_unknown(message):
    if not is_allowed(message):
        return
    bot.reply_to(message, "Perintah tidak dikenal. Ketik /help untuk daftar perintah.")

# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("[VoiceMap Bot] Starting @RadSTT_bot...")
    print(f"[VoiceMap Bot] Whitelist: {WHITELIST}")
    print(f"[VoiceMap Bot] Whisper URL: {WHISPER_URL}")
    print(f"[VoiceMap Bot] Ollama model: {OLLAMA_MODEL}")

    # Start auto-scheduler thread (fires laporan at 16:00 every day)
    sched_thread = threading.Thread(target=auto_laporan_worker, daemon=True)
    sched_thread.start()

    print("[VoiceMap Bot] Polling Telegram...")
    bot.infinity_polling(timeout=30, long_polling_timeout=20)

