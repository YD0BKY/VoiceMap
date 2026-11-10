"""
VoiceMap — whisper_server.py
Local Whisper transcription server + report storage

Usage:
    python whisper_server.py --model medium

Endpoints:
    GET  /health
    POST /transcribe
    POST /save-report         { label, transcript?, summary? }
    GET  /reports             → list of saved report labels
    GET  /reports/<file>      → download a specific report file
    POST /session/sync        { entries: [{text, timestamp_ms, timestamp_str}] }
    GET  /session             → current in-memory session entries
    DELETE /session           → clear session buffer
"""

import os
import io
import time
import tempfile
import threading
import argparse
import re
import json
from datetime import datetime, timedelta
import requests
from flask import Flask, request, jsonify, send_from_directory, Response
from flask_cors import CORS
from faster_whisper import WhisperModel

# ── Arguments ─────────────────────────────────────────────────────────────────

parser = argparse.ArgumentParser(description="VoiceMap Whisper Server")
parser.add_argument("--model", type=str, default="small",
                    choices=["tiny", "base", "small", "medium", "large-v2", "large-v3"])
parser.add_argument("--device", type=str, default="cpu", choices=["cpu", "cuda"])
parser.add_argument("--compute_type", type=str, default="int8")
parser.add_argument("--port", type=int, default=5000)
args = parser.parse_args()

MODEL_SIZE   = args.model
DEVICE       = args.device
COMPUTE_TYPE = args.compute_type
PORT         = args.port
LANGUAGE     = "id"

# Reports folder lives next to this script
REPORTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports")
CHUNKS_DIR  = os.path.join(REPORTS_DIR, "chunks")
OLLAMA_MODEL = "qwen2.5:3b-instruct-q4_K_S"
DAILY_HOUR   = 16

# ── Hallucination filter ──────────────────────────────────────────────────────
# Whisper commonly hallucinates these phrases on silence / squelch noise.
# Any transcription that matches (case-insensitive, stripped) is discarded.
HALLUCINATIONS = {
    "terima kasih",
    "terima kasih.",
    "terima kasih banyak",
    "terima kasih banyak.",
    "thank you",
    "thank you.",
    "thanks",
    "thanks.",
    "you",
    ".",
    "...",
    "uh",
    "um",
    "hmm",
    "nama puan monitor pusat ke-10, nama ke-10",
    "nama puan monitor pusat ke-10, nama ke-10.",
    "nama puan monitor pusat",
    "nama puan monitor",
}

def is_hallucination(text: str) -> bool:
    cleaned = text.strip().lower().rstrip('.').strip()
    return any(cleaned.startswith(h.rstrip('.').strip()) for h in HALLUCINATIONS)

# ── Load Whisper model ────────────────────────────────────────────────────────

print(f"[VoiceMap] Initializing Whisper '{MODEL_SIZE}' model...")
model = None

if DEVICE == "cuda":
    for comp in [COMPUTE_TYPE, "float16", "int8_float32", "int8"]:
        try:
            print(f"[VoiceMap] Trying CUDA with compute_type={comp}...")
            model = WhisperModel(MODEL_SIZE, device="cuda", compute_type=comp)
            DEVICE = "cuda"
            COMPUTE_TYPE = comp
            print(f"[VoiceMap] OK - Loaded on CUDA ({comp})")
            break
        except Exception as e:
            print(f"[VoiceMap] CUDA failed ({comp}): {e}")

if model is None:
    if args.device == "cuda":
        print("[VoiceMap] WARNING: CUDA unavailable - falling back to CPU.")
    try:
        print(f"[VoiceMap] Loading on CPU (int8)...")
        model = WhisperModel(MODEL_SIZE, device="cpu", compute_type="int8", cpu_threads=8)
        DEVICE = "cpu"
        COMPUTE_TYPE = "int8"
        print(f"[VoiceMap] OK - Loaded on CPU (int8)")
    except Exception as e:
        print(f"[VoiceMap] Fatal: {e}")
        raise

print(f"[VoiceMap] Ready - http://localhost:{PORT}")

# ── Flask ─────────────────────────────────────────────────────────────────────

app = Flask(__name__)
CORS(app)
model_lock = threading.Lock()

# ── Session Buffer (for Telegram bot access) ──────────────────────────────────
# Thread-safe in-memory store. Populated via:
#   1. /session/sync  — browser pushes its localStorage entries here
#   2. /transcribe    — new transcriptions appended as they arrive
# This does NOT affect the existing transcription flow in any way.

session_entries = []   # list of {text, timestamp_ms, timestamp_str}
session_lock    = threading.Lock()


# ── Health ────────────────────────────────────────────────────────────────────

@app.route("/health", methods=["GET"])
def health():
    busy = model_lock.locked()
    return jsonify({
        "status":   "busy" if busy else "ok",
        "model":    MODEL_SIZE,
        "device":   DEVICE,
        "language": LANGUAGE,
        "busy":     busy,
    })


# ── Ollama Proxy ──────────────────────────────────────────────────────────────

OLLAMA_URL = "http://localhost:11434"

@app.route("/ollama/api/tags", methods=["GET"])
def ollama_tags():
    try:
        r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=3)
        return jsonify(r.json()), r.status_code
    except Exception as e:
        return jsonify({"error": f"Ollama unreachable: {str(e)}"}), 503

@app.route("/ollama/api/generate", methods=["POST"])
def ollama_generate():
    try:
        data = request.json or {}
        r = requests.post(f"{OLLAMA_URL}/api/generate", json=data, stream=True, timeout=600)
        
        def generate_stream():
            for line in r.iter_lines():
                if line:
                    yield line + b"\n"
                    
        return Response(generate_stream(), status=r.status_code, content_type=r.headers.get('content-type', 'application/json'))
    except Exception as e:
        return jsonify({"error": f"Ollama error: {str(e)}"}), 503


# ── Transcribe ────────────────────────────────────────────────────────────────

@app.route("/transcribe", methods=["POST"])
def transcribe():
    if "audio" not in request.files:
        return jsonify({"error": "No audio file"}), 400

    audio_file  = request.files["audio"]
    audio_bytes = audio_file.read()

    if len(audio_bytes) < 1000:
        return jsonify({"text": "", "segments": [], "duration_ms": 0})

    print(f"[Whisper] Received {len(audio_bytes)} bytes for transcription")
    tmp_path = None
    try:
        filename = audio_file.filename or "chunk.webm"
        ext = os.path.splitext(filename)[1] or ".webm"
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
            tmp.write(audio_bytes)
            tmp_path = tmp.name

        start = time.time()

        with model_lock:
            segments, info = model.transcribe(
                tmp_path,
                language=LANGUAGE,
                beam_size=1,
                temperature=0.0,
                vad_filter=True,
                vad_parameters=dict(
                    min_silence_duration_ms=500,
                    threshold=0.55,
                ),
                condition_on_previous_text=False,
            )
            result_segments = []
            full_text_parts = []
            for seg in segments:
                text = seg.text.strip()
                if text and not is_hallucination(text):
                    result_segments.append({
                        "start": round(seg.start, 2),
                        "end":   round(seg.end,   2),
                        "text":  text,
                    })
                    full_text_parts.append(text)
                elif text:
                    print(f"[Whisper] Hallucination filtered: '{text}'")

        elapsed_ms = int((time.time() - start) * 1000)
        full_text  = " ".join(full_text_parts)
        snippet    = full_text[:80] + ("..." if len(full_text) > 80 else "")
        print(f"[Whisper] {elapsed_ms}ms -> {snippet}")

        # ── Buffer for Telegram bot (non-blocking) ────────────────────────────
        if full_text:
            now_dt = datetime.now()
            # Try parsing timestamp from chunk filename (e.g. chunk_20260826_162000.wav)
            try:
                base_fn = os.path.basename(filename)
                if "chunk_" in base_fn:
                    parts = base_fn.replace("chunk_", "").split(".")[0].split("_")
                    if len(parts) >= 2:
                        d_str, t_str = parts[0], parts[1]
                        if len(d_str) == 8 and len(t_str) == 6:
                            now_dt = datetime.strptime(f"{d_str}_{t_str}", "%Y%m%d_%H%M%S")
            except Exception:
                pass

            entry = {
                "text":          full_text,
                "timestamp_ms":  int(now_dt.timestamp() * 1000),
                "timestamp_str": now_dt.strftime("%H:%M:%S"),
            }
            with session_lock:
                session_entries.append(entry)
                # Auto-purge session entries older than 24 hours (86,400,000 ms)
                cutoff_ms = int((datetime.now().timestamp() - 86400) * 1000)
                session_entries[:] = [e for e in session_entries if e.get("timestamp_ms", 0) >= cutoff_ms]
        # ─────────────────────────────────────────────────────────────────────

        return jsonify({
            "text":        full_text,
            "segments":    result_segments,
            "duration_ms": elapsed_ms,
            "audio_lang":  info.language,
            "lang_prob":   round(info.language_probability, 3),
        })

    except Exception as e:
        print(f"[Whisper] ERROR: {e}")
        return jsonify({"error": str(e)}), 500

    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)


# ── Report Storage ────────────────────────────────────────────────────────────

@app.route("/save-report", methods=["POST"])
def save_report():
    """
    Body: { "label": "2026-06-25", "transcript": "...", "summary": "..." }
    Saves transcript and/or summary to reports/ folder.
    """
    data       = request.json or {}
    label      = data.get("label", datetime.now().strftime("%Y-%m-%d_%H%M"))
    transcript = data.get("transcript", "")
    summary    = data.get("summary", "")

    os.makedirs(REPORTS_DIR, exist_ok=True)
    saved = []

    if transcript:
        fname = f"transcript_{label}.txt"
        with open(os.path.join(REPORTS_DIR, fname), "w", encoding="utf-8") as f:
            f.write(transcript)
        saved.append(fname)
        print(f"[Reports] Saved transcript -> {fname}")

    if summary:
        fname = f"summary_{label}.txt"
        with open(os.path.join(REPORTS_DIR, fname), "w", encoding="utf-8") as f:
            f.write(summary)
        saved.append(fname)
        print(f"[Reports] Saved summary   -> {fname}")

    return jsonify({"status": "saved", "files": saved})


@app.route("/reports", methods=["GET"])
def list_reports():
    """Returns list of saved report entries, grouped by label, newest first."""
    if not os.path.exists(REPORTS_DIR):
        return jsonify([])

    files = [f for f in os.listdir(REPORTS_DIR) if f.endswith(".txt")]

    labels = {}
    for f in files:
        for prefix in ("transcript_", "summary_"):
            if f.startswith(prefix):
                label = f[len(prefix):-4]  # strip prefix and .txt
                if label not in labels:
                    labels[label] = {}
                key = "transcript" if prefix == "transcript_" else "summary"
                labels[label][key] = f

    result = []
    for label in sorted(labels.keys(), reverse=True):
        entry = {"label": label, **labels[label]}
        # Count lines in transcript for display
        tf = labels[label].get("transcript")
        if tf:
            try:
                with open(os.path.join(REPORTS_DIR, tf), "r", encoding="utf-8") as fh:
                    entry["lines"] = sum(1 for line in fh if line.strip())
            except Exception:
                entry["lines"] = 0
        result.append(entry)

    return jsonify(result)


@app.route("/reports/<filename>", methods=["GET"])
def get_report_file(filename):
    """Serve a specific report file for download."""
    return send_from_directory(REPORTS_DIR, filename, as_attachment=True)


# ── Session Sync (browser → server, for Telegram bot) ────────────────────────

@app.route("/session/clear", methods=["POST"])
def clear_session_post():
    with session_lock:
        count = len(session_entries)
        session_entries.clear()
    return jsonify({"status": "cleared", "removed": count})


@app.route("/session/sync", methods=["POST"])
def session_sync():
    """
    Browser posts its localStorage entries here so the Telegram bot
    can access them. Merges by timestamp_ms to avoid duplicates.
    Server-side: silently rejects entries older than the operational cutoff
    (16:00 yesterday/today) so stale browser archive data cannot pollute the feed.
    Body: [ {text, timestamp_ms, timestamp_str}, ... ]
    """
    incoming = request.json
    if not isinstance(incoming, list):
        return jsonify({"error": "Expected a JSON array"}), 400

    # Calculate operational cutoff: 16:00 today if past 16:00, else 16:00 yesterday
    DAILY_HOUR = 16
    now_dt  = datetime.now()
    cutoff  = now_dt.replace(hour=DAILY_HOUR, minute=0, second=0, microsecond=0)
    if cutoff > now_dt:
        cutoff -= timedelta(days=1)
    cutoff_ms = int(cutoff.timestamp() * 1000)

    added = 0
    rejected = 0
    with session_lock:
        existing_ms = {e["timestamp_ms"] for e in session_entries}
        for entry in incoming:
            ts = entry.get("timestamp_ms")
            if not ts:
                continue
            ts = int(ts)
            # Reject entries older than operational cutoff
            if ts < cutoff_ms:
                rejected += 1
                continue
            if ts not in existing_ms:
                session_entries.append({
                    "text":          str(entry.get("text", "")).strip(),
                    "timestamp_ms":  ts,
                    "timestamp_str": str(entry.get("timestamp_str", "") or entry.get("timestamp", "")),
                })
                existing_ms.add(ts)
                added += 1
        session_entries.sort(key=lambda x: x["timestamp_ms"])

    if rejected:
        print(f"[Session] Sync: +{added} new, {rejected} rejected (older than {cutoff.strftime('%Y-%m-%d %H:%M')}), total={len(session_entries)}")
    else:
        print(f"[Session] Sync: +{added} new entries, total={len(session_entries)}")
    return jsonify({"status": "ok", "added": added, "total": len(session_entries)})


@app.route("/session", methods=["GET"])
def get_session():
    """
    Returns session entries, optionally filtered by time range.
    Query params: from_ms, to_ms (Unix ms)
    """
    from_ms = request.args.get("from_ms", 0,                          type=int)
    to_ms   = request.args.get("to_ms",   int(time.time() * 1000),   type=int)
    with session_lock:
        filtered = [
            e for e in session_entries
            if from_ms <= e["timestamp_ms"] <= to_ms
        ]
    return jsonify(filtered)


@app.route("/session", methods=["DELETE"])
def clear_session():
    """Clears the in-memory session buffer."""
    with session_lock:
        count = len(session_entries)
        session_entries.clear()
    print(f"[Session] Cleared {count} entries")
    return jsonify({"status": "cleared", "removed": count})


# ── Chunked Auto-Summarizer & Compilation ──────────────────────────────────────

def call_ollama_generate(prompt):
    try:
        r = requests.post(
            f"{OLLAMA_URL}/api/generate",
            json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False,
                  "options": {"temperature": 0.6, "num_predict": 2048}},
            timeout=600
        )
        if r.status_code == 200:
            return r.json().get("response", "").strip()
        else:
            print(f"[Ollama Helper] HTTP {r.status_code}")
            return None
    except Exception as e:
        print(f"[Ollama Helper] Error: {e}")
        return None

def get_completed_2h_blocks(from_ms, to_ms):
    from_dt = datetime.fromtimestamp(from_ms / 1000.0)
    to_dt = datetime.fromtimestamp(to_ms / 1000.0)
    
    align_dt = from_dt.replace(hour=DAILY_HOUR, minute=0, second=0, microsecond=0)
    if align_dt > from_dt:
        align_dt -= timedelta(days=1)
        
    blocks = []
    curr_dt = align_dt
    now_dt = datetime.now()
    
    while curr_dt + timedelta(hours=2) <= to_dt:
        next_dt = curr_dt + timedelta(hours=2)
        if curr_dt >= from_dt and next_dt <= to_dt and next_dt <= now_dt:
            blocks.append((curr_dt, next_dt))
        curr_dt = next_dt
        
    return blocks

def auto_summarize_worker():
    print("[AutoSummarizer] Background thread started.")
    while True:
        try:
            now = datetime.now()
            from_dt = now - timedelta(hours=24)
            align_dt = from_dt.replace(hour=DAILY_HOUR, minute=0, second=0, microsecond=0)
            if align_dt > from_dt:
                align_dt -= timedelta(days=1)
                
            from_ms = int(align_dt.timestamp() * 1000)
            to_ms = int(now.timestamp() * 1000)
            
            blocks = get_completed_2h_blocks(from_ms, to_ms)
            os.makedirs(CHUNKS_DIR, exist_ok=True)
            
            for start_dt, end_dt in blocks:
                label = f"{start_dt.strftime('%Y%m%d_%H%M')}_to_{end_dt.strftime('%H%M')}"
                sum_path = os.path.join(CHUNKS_DIR, f"summary_2h_{label}.txt")
                trans_path = os.path.join(CHUNKS_DIR, f"transcript_2h_{label}.txt")
                
                start_ms = int(start_dt.timestamp() * 1000)
                end_ms = int(end_dt.timestamp() * 1000)
                
                with session_lock:
                    block_entries = [e for e in session_entries if start_ms <= e["timestamp_ms"] < end_ms]
                    
                if not block_entries:
                    # Genuinely no entries in memory. If the server restarted and is out of sync,
                    # we do NOT write any files. This prevents saving empty placeholders.
                    continue
                    
                current_trans = "\n".join(f"[{e['timestamp_str']}] {e['text']}" for e in block_entries)
                
                # Check if existing files match the current transcript text
                if os.path.exists(trans_path) and os.path.exists(sum_path):
                    try:
                        with open(trans_path, "r", encoding="utf-8") as f:
                            existing_trans = f.read()
                        if existing_trans.strip() == current_trans.strip():
                            # Already generated and matches the content, skip
                            continue
                    except Exception:
                        pass
        except Exception as e:
            print(f"[AutoSummarizer] Error in loop: {e}")
            
        time.sleep(3600)  # Check once per hour to save CPU


@app.route("/reports/compile-summary", methods=["POST"])
def compile_summary():
    """
    Compiles the entire transcript within the given time range in a single-pass prompt
    to save CPU time and improve summary coherence.
    """
    data = request.json or {}
    from_ms = data.get("from_ms")
    to_ms = data.get("to_ms")
    label = data.get("label", "")
    
    if not from_ms or not to_ms:
        return jsonify({"error": "Missing from_ms or to_ms"}), 400
        
    from_dt = datetime.fromtimestamp(from_ms / 1000.0)
    to_dt = datetime.fromtimestamp(to_ms / 1000.0)
    
    print(f"[Compile] Compiling single-pass summary for {from_dt} to {to_dt}")
    
    # Fetch entries from memory buffer within the range
    with session_lock:
        range_entries = [e for e in session_entries if from_ms <= e["timestamp_ms"] <= to_ms]
        
    if not range_entries:
        # Fallback: check if we have any archived file matching the label/date
        # or just try to load from the reports directory
        # Let's check reports folder for a matching transcript file
        transcript_text = ""
        try:
            date_str = from_dt.strftime("%Y-%m-%d")
            tf_path = os.path.join(REPORTS_DIR, f"transcript_{date_str}.txt")
            if os.path.exists(tf_path):
                with open(tf_path, "r", encoding="utf-8") as f:
                    raw_text = f.read()
                
                # Split and filter lines by time
                filtered_lines = []
                for line in raw_text.splitlines():
                    line = line.strip()
                    if not line or line.startswith("VoiceMap") or line.startswith("="):
                        continue
                    if line.startswith("[") and "]" in line:
                        ts_str = line[1:line.index("]")]
                        text   = line[line.index("]")+1:].strip()
                        
                        try:
                            full_ts_str = f"{date_str} {ts_str}"
                            line_dt = None
                            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %I:%M:%S %p", "%Y-%m-%d %H:%M"):
                                try:
                                    line_dt = datetime.strptime(full_ts_str, fmt)
                                    break
                                except ValueError:
                                    pass
                                    
                            if line_dt and (from_dt <= line_dt <= to_dt):
                                filtered_lines.append(f"[{ts_str}] {text}")
                        except Exception:
                            pass
                            
                transcript_text = "\n".join(filtered_lines)
        except Exception as e:
            print(f"[Compile] Failed to read/filter archived transcript: {e}")
    else:
        transcript_text = "\n".join(f"[{e['timestamp_str']}] {e['text']}" for e in range_entries)
        
    if not transcript_text or not transcript_text.strip():
        return jsonify({
            "status": "compiled",
            "summary": "Tidak ada aktivitas komunikasi radio penting pada periode ini untuk dirangkum.",
            "executive": "Tidak ada aktivitas.",
            "details": ""
        })
        
    # We send the entire transcript in a single prompt to save CPU
    prompt = (
        f"Kamu adalah analis komunikasi radio Indonesia. Berikut adalah transkripsi komunikasi radio untuk periode: {label}.\n\n"
        f"Transkripsi:\n---\n{transcript_text}\n---\n\n"
        f"Buatlah laporan ringkasan terstruktur dalam Bahasa Indonesia yang mencakup:\n"
        f"1. RINGKASAN EKSEKUTIF (3-4 kalimat ringkasan umum seluruh aktivitas)\n"
        f"2. DETAIL KEJADIAN PENTING (Daftar poin-poin aktivitas penting beserta waktunya)\n\n"
        f"Tulis dengan format markdown yang rapi. /no_think"
    )
    
    print("[Compile] Generating summary via Ollama...")
    final_compiled = call_ollama_generate(prompt)
    
    if not final_compiled:
        return jsonify({"error": "Failed to generate summary from Ollama"}), 500
        
    # Extract executive summary (first few paragraphs) to return separately if needed by UI/bot
    executive_summary = ""
    lines = final_compiled.split("\n")
    exec_parts = []
    capture = False
    for line in lines:
        if "RINGKASAN EKSEKUTIF" in line.upper():
            capture = True
            continue
        if "DETAIL" in line.upper() or "POIN" in line.upper():
            capture = False
        if capture and line.strip():
            exec_parts.append(line.strip())
            
    executive_summary = "\n".join(exec_parts) if exec_parts else final_compiled[:500] + "..."
    
    return jsonify({
        "status": "compiled",
        "summary": final_compiled,
        "executive": executive_summary,
        "details": final_compiled
    })
# ── Root info page ────────────────────────────────────────────────────────────

@app.route("/", methods=["GET"])
def index():
    with session_lock:
        sess_count = len(session_entries)
    return """
    <html><body style="font-family:monospace;background:#060812;color:#94a3b8;padding:32px">
    <h2 style="color:#a78bfa">🎙️ VoiceMap Whisper Server</h2>
    <p>Status: <span style="color:#34d399">✅ Running</span></p>
    <p>Model: <b style="color:#f1f5f9">{model}</b> | Lang: <b style="color:#f1f5f9">ID</b></p>
    <p>VoiceMap: <a href="http://localhost:8765" style="color:#38bdf8">http://localhost:8765</a></p>
    <p>Reports: <code style="color:#38bdf8">{reports}</code></p>
    <p>Session buffer: <b style="color:#f1f5f9">{sess}</b> entries</p>
    </body></html>
    """.format(model=MODEL_SIZE, reports=REPORTS_DIR, sess=sess_count)


if __name__ == "__main__":
    t = threading.Thread(target=auto_summarize_worker, daemon=True)
    t.start()
    
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)
