/**
 * VoiceMap — app.js
 * Local Whisper transcription + Ollama AI summary + daily auto-report
 */

'use strict';

// ─── Config ───────────────────────────────────────────────────────────────────

const WHISPER_URL       = 'http://10.25.151.59:5000';
const OLLAMA_URL        = `${WHISPER_URL}/ollama`;
const OLLAMA_DEF_MODEL  = 'qwen2.5:1.5b';
const CHUNK_MS          = 60000;   // 60-second recording chunks
const DAILY_HOUR        = 16;      // auto-report triggers at 16:00

// ─── State ────────────────────────────────────────────────────────────────────

const state = {
  isListening:      false,
  mediaRecorder:    null,
  audioStream:      null,
  chunkTimer:       null,

  entries:          [],    // { id, text, timestamp, timestampMs }
  totalEntries:     0,

  audioCtx:         null,
  levelAnimId:      null,

  whisperOnline:    false,
  ollamaOnline:     false,
  ollamaPollTimer:  null,
  lastSummaryText:  '',
};

// ─── DOM refs ─────────────────────────────────────────────────────────────────

const $  = id => document.getElementById(id);
const whisperPill     = $('whisper-pill');
const whisperPillText = $('whisper-pill-text');
const interimBar      = $('interim-bar');
const interimText     = $('interim-text');
const feed            = $('feed');
const feedEmpty       = $('feed-empty');
const feedCount       = $('feed-count');
const feedPeriodLabel = $('feed-period-label');
const levelBar        = $('level-bar');
const levelLabel      = $('level-label');
const reportBtn       = $('report-btn');
const reportModal     = $('report-modal');
const reportModalClose= $('report-modal-close');
const reportFrom      = $('report-from');
const reportTo        = $('report-to');
const ollamaModel     = $('ollama-model');
const ollamaDot       = $('ollama-dot');
const ollamaStatusTxt = $('ollama-status-text');
const dlTranscriptBtn = $('dl-transcript-btn');
const genSummaryBtn   = $('gen-summary-btn');
const summaryOutput   = $('summary-output');
const savedReportsList= $('saved-reports-list');
const refreshReportsBtn=$('refresh-reports-btn');
const toastEl         = $('toast');

// ─── Animated Background ──────────────────────────────────────────────────────

(function initCanvas() {
  const canvas = $('bg-canvas');
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  let p = [];
  const resize = () => { canvas.width = innerWidth; canvas.height = innerHeight; };
  const mkP = () => ({
    x: Math.random() * canvas.width, y: Math.random() * canvas.height,
    r: Math.random() * 180 + 60,
    dx: (Math.random() - .5) * .12, dy: (Math.random() - .5) * .12,
    hue: Math.random() < .5 ? 265 : 200, a: Math.random() * .09 + .03
  });
  const draw = () => {
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    for (const q of p) {
      const g = ctx.createRadialGradient(q.x, q.y, 0, q.x, q.y, q.r);
      g.addColorStop(0, `hsla(${q.hue},78%,65%,${q.a})`);
      g.addColorStop(1, 'transparent');
      ctx.fillStyle = g; ctx.beginPath(); ctx.arc(q.x, q.y, q.r, 0, Math.PI * 2); ctx.fill();
      q.x += q.dx; q.y += q.dy;
      if (q.x < -q.r) q.x = canvas.width + q.r;
      if (q.x > canvas.width + q.r) q.x = -q.r;
      if (q.y < -q.r) q.y = canvas.height + q.r;
      if (q.y > canvas.height + q.r) q.y = -q.r;
    }
    requestAnimationFrame(draw);
  };
  resize(); p = Array.from({ length: 6 }, mkP); draw();
  window.addEventListener('resize', () => { resize(); p = Array.from({ length: 6 }, mkP); });
})();

// ─── Helpers ──────────────────────────────────────────────────────────────────

function escH(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}

function getDateStr(ms) {
  const d = new Date(ms || Date.now());
  return d.getFullYear() + '-' +
    String(d.getMonth() + 1).padStart(2, '0') + '-' +
    String(d.getDate()).padStart(2, '0');
}

function toDatetimeLocal(d) {
  const pad = n => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

let toastTimer;
function showToast(msg, dur = 3000) {
  toastEl.textContent = msg;
  toastEl.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toastEl.classList.remove('show'), dur);
}

// ─── Audio Level Meter ────────────────────────────────────────────────────────

function startLevelMeter(stream) {
  if (state.levelAnimId) cancelAnimationFrame(state.levelAnimId);
  if (state.audioCtx) state.audioCtx.close();
  const ctx = new (window.AudioContext || window.webkitAudioContext)();
  state.audioCtx = ctx;
  const analyser = ctx.createAnalyser();
  analyser.fftSize = 256;
  ctx.createMediaStreamSource(stream).connect(analyser);
  const data = new Uint8Array(analyser.frequencyBinCount);
  const tick = () => {
    state.levelAnimId = requestAnimationFrame(tick);
    analyser.getByteFrequencyData(data);
    const pct = Math.min(100, data.reduce((a, b) => a + b, 0) / data.length * 2.2);
    levelBar.style.width = pct + '%';
    if (pct < 2)       { levelLabel.textContent = 'No signal'; levelLabel.style.color = ''; }
    else if (pct < 30) { levelLabel.textContent = 'Low';       levelLabel.style.color = '#fbbf24'; }
    else if (pct < 70) { levelLabel.textContent = 'Good';      levelLabel.style.color = '#34d399'; }
    else               { levelLabel.textContent = 'Strong';    levelLabel.style.color = '#38bdf8'; }
  };
  tick();
}

function stopLevelMeter() {
  if (state.levelAnimId) { cancelAnimationFrame(state.levelAnimId); state.levelAnimId = null; }
  if (state.audioCtx) { state.audioCtx.close(); state.audioCtx = null; }
  levelBar.style.width = '0%';
  levelLabel.textContent = 'No signal';
  levelLabel.style.color = '';
}

// ─── Feed ─────────────────────────────────────────────────────────────────────

function addFeedEntry(text, tsStr, tsMs) {
  // Remove empty placeholder
  if (feedEmpty && !feedEmpty.hidden) feedEmpty.hidden = true;

  const div = document.createElement('div');
  div.className = 'feed-entry';
  div.innerHTML = `<span class="fe-time">${escH(tsStr)}</span><span class="fe-text">${escH(text)}</span>`;
  feed.appendChild(div);
  feed.scrollTop = feed.scrollHeight;

  state.entries.push({ id: state.totalEntries++, text, timestamp: tsStr, timestampMs: tsMs });
  updateFeedMeta();
  saveSession();
}

function updateFeedMeta() {
  feedCount.textContent = `${state.entries.length.toLocaleString()} entri`;
}

function updateFeedPeriod() {
  const now = new Date();
  const cutoff = new Date(now);
  cutoff.setHours(DAILY_HOUR, 0, 0, 0);
  const isAfter16 = now >= cutoff;
  feedPeriodLabel.textContent = `Periode: 16:00 ${isAfter16 ? 'hari ini' : 'kemarin'} → sekarang`;
}

// ─── Recording ────────────────────────────────────────────────────────────────

// ─── Polling System (Viewer Mode) ─────────────────────────────────────────────

function getOperationalCutoffMs() {
  const now = new Date();
  const cutoff = new Date(now);
  cutoff.setHours(DAILY_HOUR, 0, 0, 0);
  if (cutoff > now) {
    cutoff.setDate(cutoff.getDate() - 1);
  }
  return cutoff.getTime();
}

async function pollServerSession() {
  if (!state.whisperOnline) {
    console.log("[Poll] Whisper offline pill is active, skipping poll.");
    return;
  }

  try {
    const cutoffMs = getOperationalCutoffMs();
    const res = await fetch(`${WHISPER_URL}/session?from_ms=${cutoffMs}`);
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const entries = await res.json();

    if (entries && entries.length > 0) {
      if (feedEmpty && !feedEmpty.hidden) feedEmpty.hidden = true;

      const existingMs = new Set(state.entries.map(e => e.timestampMs));
      let addedAny = false;
      let needsRebuild = false;
      const lastMs = state.entries.length > 0
        ? state.entries[state.entries.length - 1].timestampMs : 0;

      for (const e of entries) {
        if (e.timestamp_ms < cutoffMs) continue;
        if (existingMs.has(e.timestamp_ms)) continue;

        addedAny = true;
        existingMs.add(e.timestamp_ms);

        // If this new entry is older than the last rendered one, we need a full rebuild
        if (e.timestamp_ms < lastMs) needsRebuild = true;

        state.entries.push({
          id: state.totalEntries++,
          text: e.text,
          timestamp: e.timestamp_str,
          timestampMs: e.timestamp_ms
        });
      }

      if (addedAny) {
        // Always keep state sorted
        state.entries.sort((a, b) => a.timestampMs - b.timestampMs);

        if (needsRebuild) {
          // Rebuild entire DOM in correct chronological order
          feed.innerHTML = '';
          for (const e of state.entries) {
            const div = document.createElement('div');
            div.className = 'feed-entry';
            div.dataset.ts = String(e.timestampMs);
            div.innerHTML = `<span class="fe-time">${escH(e.timestamp)}</span><span class="fe-text">${escH(e.text)}</span>`;
            feed.appendChild(div);
          }
        } else {
          // Append only new entries (already in order at the end)
          for (const e of state.entries) {
            if (!feed.querySelector(`[data-ts="${e.timestampMs}"]`)) {
              const div = document.createElement('div');
              div.className = 'feed-entry';
              div.dataset.ts = String(e.timestampMs);
              div.innerHTML = `<span class="fe-time">${escH(e.timestamp)}</span><span class="fe-text">${escH(e.text)}</span>`;
              feed.appendChild(div);
            }
          }
        }

        feed.scrollTop = feed.scrollHeight;
        updateFeedMeta();
        saveSession();
      }
    }
  } catch(e) {
    console.warn('[Poll] Failed to fetch live transcripts:', e);
  }
}

// Start polling immediately and repeat every 3 seconds
setInterval(pollServerSession, 3000);

// ─── Whisper Health Poll ──────────────────────────────────────────────────────

async function checkWhisper() {
  try {
    const res = await fetch(`${WHISPER_URL}/health`, { signal: AbortSignal.timeout(15000) });
    if (res.ok) {
      const d = await res.json();
      state.whisperOnline = true;
      if (d.busy) {
        whisperPill.className = 'whisper-pill online';
        whisperPillText.textContent = `Whisper ⏳ ${d.model} (busy)`;
      } else {
        whisperPill.className = 'whisper-pill online';
        whisperPillText.textContent = `Whisper ✅ ${d.model}`;
      }
    } else throw new Error();
  } catch(_) {
    state.whisperOnline = false;
    whisperPill.className = 'whisper-pill offline';
    whisperPillText.textContent = 'Whisper ❌ Offline';
  }
}

setInterval(checkWhisper, 10000);

// Re-check immediately when the browser tab becomes visible again.
// This prevents the stale "offline" indicator caused by JS timer throttling
// when the tab is in the background.
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') {
    checkWhisper();
    syncSessionToServer();
  }
});

// ─── Ollama Health Poll ───────────────────────────────────────────────────────

async function checkOllama() {
  try {
    const res = await fetch(`${OLLAMA_URL}/api/tags`, { signal: AbortSignal.timeout(3000) });
    state.ollamaOnline = res.ok;
  } catch(_) {
    state.ollamaOnline = false;
  }
  if (ollamaDot) {
    ollamaDot.className = 'ollama-dot ' + (state.ollamaOnline ? 'online' : 'offline');
    ollamaStatusTxt.textContent = state.ollamaOnline ? 'Ollama ✅ Online' : 'Ollama ❌ Offline — jalankan: ollama serve';
  }
}

// ─── Session Persistence (localStorage) ──────────────────────────────────────

const SESSION_KEY = 'voicemap_session';
const SESSION_MAX = 5000;

function saveSession() {
  try {
    localStorage.setItem(SESSION_KEY, JSON.stringify(state.entries.slice(-SESSION_MAX)));
  } catch(e) {
    console.warn('[Session] Save failed:', e);
  }
}

function loadSession() {
  // Always start fresh from the server — do NOT load from localStorage.
  // The server is the single source of truth; pollServerSession() will
  // populate the feed within 3 seconds of page load.
  // Clear any stale localStorage data to avoid old entries showing up.
  try {
    localStorage.removeItem(SESSION_KEY);
    // Also clear all archive keys
    const idx = getArchiveIndex();
    for (const d of idx) localStorage.removeItem(ARC_PREFIX + d);
    localStorage.removeItem(ARC_IDX_KEY);
  } catch(e) { /* ignore */ }
}


// ─── Server Sync (for Telegram bot) ──────────────────────────────────────────────────────────
// Silently pushes ONLY current-period entries to the server.
// Does NOT push archived days — those would pollute the server with old data.

async function syncSessionToServer() {
  if (!state.whisperOnline) return;
  try {
    const cutoffMs = getOperationalCutoffMs();
    // Only push entries within the current operational period
    const payload = state.entries
      .filter(e => e.text && e.timestampMs && e.timestampMs >= cutoffMs)
      .map(e => ({
        text:          e.text,
        timestamp_ms:  e.timestampMs,
        timestamp_str: e.timestamp || '',
      }));

    if (!payload.length) return;

    await fetch(`${WHISPER_URL}/session/sync`, {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify(payload),
    });
    console.log(`[Sync] Pushed ${payload.length} current-period entries to server`);
  } catch(e) {
    console.warn('[Sync] Background sync failed (non-critical):', e.message);
  }
}

// Sync every 5 minutes
setInterval(syncSessionToServer, 5 * 60 * 1000);

// ─── Archive System (multi-day) ───────────────────────────────────────────────

const ARC_PREFIX   = 'voicemap_arc_';
const ARC_IDX_KEY  = 'voicemap_arc_index';
const ARC_MAX_DAYS = 30;

function getArchiveIndex() {
  try { return JSON.parse(localStorage.getItem(ARC_IDX_KEY) || '[]'); } catch { return []; }
}

function loadArchiveDayRaw(dateStr) {
  try { return JSON.parse(localStorage.getItem(ARC_PREFIX + dateStr) || 'null'); } catch { return null; }
}

function saveArchiveDay(dateStr, entries) {
  if (!entries.length) return;
  try {
    const existing = loadArchiveDayRaw(dateStr) || [];
    const byId = new Map();
    for (const e of [...existing, ...entries]) byId.set(e.id, e);
    const merged = [...byId.values()].sort((a, b) => (a.timestampMs || 0) - (b.timestampMs || 0));
    localStorage.setItem(ARC_PREFIX + dateStr, JSON.stringify(merged));

    const idx = getArchiveIndex();
    if (!idx.includes(dateStr)) {
      idx.push(dateStr); idx.sort();
      while (idx.length > ARC_MAX_DAYS) localStorage.removeItem(ARC_PREFIX + idx.shift());
      localStorage.setItem(ARC_IDX_KEY, JSON.stringify(idx));
    }
  } catch(e) { console.warn('[Archive] Failed:', e); }
}

function archiveOldEntries() {
  const today = getDateStr();
  const byDate = {};
  for (const e of state.entries) {
    const d = getDateStr(e.timestampMs);
    if (!byDate[d]) byDate[d] = [];
    byDate[d].push(e);
  }
  let archived = false;
  for (const [d, entries] of Object.entries(byDate)) {
    if (d !== today) { saveArchiveDay(d, entries); archived = true; }
  }
  if (archived) {
    state.entries = state.entries.filter(e => getDateStr(e.timestampMs) === today);
    saveSession();
  }
}

// Midnight rollover check every 5 min
let _lastDate = getDateStr();
setInterval(() => {
  const today = getDateStr();
  if (today !== _lastDate) { _lastDate = today; archiveOldEntries(); }
}, 5 * 60 * 1000);

// ─── Build Transcript Text ────────────────────────────────────────────────────

function buildTranscriptText(fromMs, toMs, entries) {
  const src = entries || state.entries;
  const filtered = src.filter(e => e.timestampMs >= fromMs && e.timestampMs <= toMs);
  if (!filtered.length) return null;
  return filtered.map(e => `[${e.timestamp}] ${e.text}`).join('\n');
}

// ─── Report Defaults (16:00 yesterday → now) ──────────────────────────────────

function getReportRange() {
  const now = new Date();
  const to = new Date(now);
  const from = new Date(now);
  from.setDate(from.getDate() - 1);
  from.setHours(DAILY_HOUR, 0, 0, 0);
  return { from, to };
}

// ─── Download Transcript ──────────────────────────────────────────────────────

function downloadTranscript() {
  const fromMs = new Date(reportFrom.value).getTime();
  const toMs   = new Date(reportTo.value).getTime();
  if (isNaN(fromMs) || isNaN(toMs)) { showToast('Pilih rentang waktu yang valid.'); return; }

  // Combine live entries + all archives
  const allEntries = collectAllEntries(fromMs, toMs);
  const text = buildTranscriptText(fromMs, toMs, allEntries);
  if (!text) { showToast('Tidak ada transkripsi dalam periode ini.', 4000); return; }

  const label = `${reportFrom.value.replace('T', ' ')} s/d ${reportTo.value.replace('T', ' ')}`;
  const header = `VoiceMap — Transkripsi Radio\nPeriode: ${label}\nDibuat: ${new Date().toLocaleString('id-ID')}\n${'='.repeat(60)}\n\n`;
  const blob = new Blob([header + text], { type: 'text/plain;charset=utf-8' });
  const fname = `transcript_${getDateStr()}_${Date.now()}.txt`;
  const a = Object.assign(document.createElement('a'), { href: URL.createObjectURL(blob), download: fname });
  a.click(); URL.revokeObjectURL(a.href);
  showToast('Transcript diunduh!');
}

// Collect entries from live session + all archives within the time range
function collectAllEntries(fromMs, toMs) {
  const all = [...state.entries];
  for (const dateStr of getArchiveIndex()) {
    const d = loadArchiveDayRaw(dateStr);
    if (d) for (const e of d) all.push(e);
  }
  // Deduplicate by id
  const byId = new Map();
  for (const e of all) byId.set(e.id, e);
  return [...byId.values()].sort((a, b) => (a.timestampMs || 0) - (b.timestampMs || 0));
}

// ─── Generate AI Summary (streaming) ─────────────────────────────────────────

async function generateAndDownloadSummary(transcriptText, periodLabel, autoSave, fromMs, toMs) {
  if (!state.ollamaOnline) {
    showToast('Ollama offline! Tidak bisa generate summary.', 5000);
    return null;
  }

  let fMs = fromMs;
  let tMs = toMs;
  if (!fMs || !tMs) {
    fMs = new Date(reportFrom.value).getTime();
    tMs = new Date(reportTo.value).getTime();
  }
  
  if (isNaN(fMs) || isNaN(tMs)) {
    // fallback if no timestamps provided
    const range = getReportRange();
    fMs = range.from.getTime();
    tMs = range.to.getTime();
  }

  summaryOutput.hidden = false;
  summaryOutput.innerHTML = `<div class="sum-loading"><div class="sum-spinner"></div><p id="sum-progress">Menghubungi Server untuk kompilasi Ringkasan AI...</p></div>`;
  genSummaryBtn.disabled = true;

  try {
    const res = await fetch(`${WHISPER_URL}/reports/compile-summary`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ from_ms: fMs, to_ms: tMs, label: periodLabel })
    });
    if (!res.ok) throw new Error('HTTP ' + res.status);

    const data = await res.json();
    const summaryText = data.summary || '(Tidak ada respons dari model)';
    state.lastSummaryText = summaryText;

    const formatted = summaryText
      .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
      .replace(/^### (.+)$/gm, '<h4>$1</h4>')
      .replace(/^## (.+)$/gm, '<h3>$1</h3>')
      .replace(/^# (.+)$/gm, '<h2>$1</h2>')
      .replace(/^\d+\.\s+/gm, m => `<span class="sum-num">${m}</span>`)
      .replace(/\n/g, '<br>');
    summaryOutput.innerHTML = `<div class="sum-text">${formatted}</div>`;

    if (!autoSave) {
      // Download immediately
      const blob = new Blob([summaryText], { type: 'text/plain;charset=utf-8' });
      const a = Object.assign(document.createElement('a'), {
        href: URL.createObjectURL(blob),
        download: `summary_${getDateStr()}_${Date.now()}.txt`
      });
      a.click(); URL.revokeObjectURL(a.href);
      showToast('AI Summary diunduh! 🎉', 3000);
    }

    return summaryText;
  } catch(e) {
    summaryOutput.innerHTML = `<p class="sum-error">❌ Error: ${escH(e.message)}</p>`;
    showToast('Kompilasi error: ' + e.message, 6000);
    return null;
  } finally {
    genSummaryBtn.disabled = false;
  }
}
// ─── Report Modal ─────────────────────────────────────────────────────────────

function openReportModal() {
  const { from, to } = getReportRange();
  if (reportFrom) reportFrom.value = toDatetimeLocal(from);
  if (reportTo)   reportTo.value   = toDatetimeLocal(to);
  if (ollamaModel && !ollamaModel.value) ollamaModel.value = OLLAMA_DEF_MODEL;
  summaryOutput.hidden = true;
  summaryOutput.innerHTML = '';
  checkOllama();
  loadSavedReports();
  reportModal.hidden = false;
  document.body.style.overflow = 'hidden';
  // Poll Ollama while modal open
  if (!state.ollamaPollTimer) {
    state.ollamaPollTimer = setInterval(checkOllama, 8000);
  }
}

function closeReportModal() {
  reportModal.hidden = true;
  document.body.style.overflow = '';
  if (state.ollamaPollTimer) {
    clearInterval(state.ollamaPollTimer);
    state.ollamaPollTimer = null;
  }
}

// ─── Saved Reports (from server) ──────────────────────────────────────────────

async function loadSavedReports() {
  savedReportsList.innerHTML = '<p class="reports-loading">Memuat…</p>';
  try {
    const res = await fetch(`${WHISPER_URL}/reports`, { signal: AbortSignal.timeout(4000) });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const reports = await res.json();

    if (!reports.length) {
      savedReportsList.innerHTML = '<p class="reports-empty">Belum ada laporan tersimpan. Laporan harian otomatis disimpan setiap pukul 16:00.</p>';
      return;
    }

    savedReportsList.innerHTML = reports.map(r => {
      const hasT = !!r.transcript;
      const hasS = !!r.summary;
      const lines = r.lines ? `${r.lines.toLocaleString()} baris` : '';
      return `
        <div class="saved-report-row">
          <div class="saved-report-info">
            <span class="saved-report-label">${escH(r.label)}</span>
            ${lines ? `<span class="saved-report-meta">${escH(lines)}</span>` : ''}
          </div>
          <div class="saved-report-actions">
            ${hasT ? `<a class="btn btn-ghost btn-sm" href="${WHISPER_URL}/reports/${encodeURIComponent(r.transcript)}" download>📄 Transcript</a>` : '<span class="btn btn-ghost btn-sm disabled">📄 Transcript</span>'}
            ${hasS ? `<a class="btn btn-accent btn-sm" href="${WHISPER_URL}/reports/${encodeURIComponent(r.summary)}" download>🤖 Summary</a>` : `<button class="btn btn-accent btn-sm" onclick="genSummaryForLabel('${escH(r.label)}')">🤖 Generate</button>`}
          </div>
        </div>`;
    }).join('');
  } catch(e) {
    savedReportsList.innerHTML = `<p class="reports-error">Tidak bisa memuat laporan dari server: ${escH(e.message)}</p>`;
  }
}

async function genSummaryForLabel(label) {
  let transcriptText = '';
  const data = loadArchiveDayRaw(label);
  
  if (data && data.length) {
    transcriptText = data.map(e => `[${e.timestamp}] ${e.text}`).join('\n');
  } else {
    try {
      const res = await fetch(`${WHISPER_URL}/reports/transcript_${encodeURIComponent(label)}.txt`);
      if (!res.ok) throw new Error('HTTP status ' + res.status);
      transcriptText = await res.text();
      // Remove metadata header block if present to keep summary prompt clean
      if (transcriptText.includes('============================================================')) {
        transcriptText = transcriptText.split('============================================================\n\n').slice(1).join('');
      }
    } catch (err) {
      console.warn('[Report] Failed to fetch transcript from server:', err);
      showToast('Tidak ada data transkripsi di arsip lokal maupun di server.', 5000);
      return;
    }
  }

  if (!transcriptText || !transcriptText.trim()) {
    showToast('Data transkripsi kosong.', 4000);
    return;
  }

  // Parse historical dates in format YYYY-MM-DD
  let fromMs = null, toMs = null;
  const parts = label.split('-');
  if (parts.length === 3) {
    const toDate = new Date(parseInt(parts[0]), parseInt(parts[1]) - 1, parseInt(parts[2]), DAILY_HOUR, 0, 0, 0);
    const fromDate = new Date(toDate);
    fromDate.setDate(fromDate.getDate() - 1);
    fromMs = fromDate.getTime();
    toMs = toDate.getTime();
  }

  const summaryText = await generateAndDownloadSummary(transcriptText, label, true, fromMs, toMs);
  if (summaryText) {
    await saveReportToServer(label, null, summaryText);
    loadSavedReports();
  }
}

// ─── Save Report to Server ────────────────────────────────────────────────────

async function saveReportToServer(label, transcript, summary) {
  try {
    const res = await fetch(`${WHISPER_URL}/save-report`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ label, transcript, summary })
    });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    return true;
  } catch(e) {
    console.warn('[Report] Save to server failed:', e);
    return false;
  }
}

// ─── Daily Auto-Report at 16:00 ───────────────────────────────────────────────

async function triggerDailyReport() {
  const now = new Date();
  // Period: 16:00 yesterday → 16:00 today
  const to = new Date(now); to.setHours(DAILY_HOUR, 0, 0, 0);
  const from = new Date(to); from.setDate(from.getDate() - 1);
  const label = getDateStr(from.getTime()); // yesterday's date

  const allEntries = collectAllEntries(from.getTime(), to.getTime());
  const transcriptText = buildTranscriptText(from.getTime(), to.getTime(), allEntries);
  if (!transcriptText) {
    console.log('[AutoReport] No entries for period, skipping.');
    return;
  }

  showToast('⏰ Menyimpan laporan harian 16:00…', 5000);

  // 1. Save transcript to server
  const tSaved = await saveReportToServer(label, transcriptText, null);

  // 2. Try AI summary
  await checkOllama();
  if (state.ollamaOnline) {
    const periodLabel = `${label} 16:00 -> ${getDateStr()} 16:00`;
    const summaryText = await generateAndDownloadSummary(transcriptText, periodLabel, true, from.getTime(), to.getTime());
    if (summaryText) await saveReportToServer(label, null, summaryText);
    showToast('Laporan harian disimpan ke server!', 6000);
  } else {
    showToast('✅ Transcript disimpan. Ollama offline — buka Laporan untuk generate AI Summary.', 8000);
  }

  // 3. Archive old entries and clear feed
  archiveOldEntries();
  clearFeed();
}

function clearFeed() {
  feed.innerHTML = '';
  feed.appendChild(feedEmpty);
  feedEmpty.hidden = false;
  state.entries = [];
  state.totalEntries = 0;
  updateFeedMeta();
  saveSession();
}

// Check at 16:00 every day (poll every minute)
let _autoReportTriggeredDate = null;
setInterval(() => {
  const now = new Date();
  const todayStr = getDateStr();
  if (now.getHours() === DAILY_HOUR && now.getMinutes() === 0 && _autoReportTriggeredDate !== todayStr) {
    _autoReportTriggeredDate = todayStr;
    triggerDailyReport();
  }
}, 60 * 1000);

// ─── Event Wiring ─────────────────────────────────────────────────────────────

reportBtn.addEventListener('click', openReportModal);
reportModalClose.addEventListener('click', closeReportModal);
reportModal.addEventListener('click', e => { if (e.target === reportModal) closeReportModal(); });
document.addEventListener('keydown', e => { if (e.key === 'Escape' && !reportModal.hidden) closeReportModal(); });

dlTranscriptBtn.addEventListener('click', downloadTranscript);

genSummaryBtn.addEventListener('click', async () => {
  const fromMs = new Date(reportFrom.value).getTime();
  const toMs   = new Date(reportTo.value).getTime();
  if (isNaN(fromMs) || isNaN(toMs)) { showToast('Pilih rentang waktu yang valid.'); return; }
  const allEntries = collectAllEntries(fromMs, toMs);
  const text = buildTranscriptText(fromMs, toMs, allEntries);
  if (!text) { showToast('Tidak ada transkripsi dalam periode ini.', 4000); return; }
  const label = `${reportFrom.value.replace('T', ' ')} s/d ${reportTo.value.replace('T', ' ')}`;
  await generateAndDownloadSummary(text, label, false, fromMs, toMs);
});

if (refreshReportsBtn) refreshReportsBtn.addEventListener('click', loadSavedReports);

// ─── Init ─────────────────────────────────────────────────────────────────────

updateFeedPeriod();
checkWhisper();
loadSession();

// Call polling once immediately on startup
setTimeout(pollServerSession, 100);

// Push overnight localStorage data to server so the Telegram bot can read it.
// Runs after a short delay to let checkWhisper() confirm the server is online first.
setTimeout(syncSessionToServer, 3000);

setTimeout(() => {
  showToast('Viewer Aktif. Menampilkan transkripsi real-time.', 4000);
}, 1500);
