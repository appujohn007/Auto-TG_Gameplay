"""
Auto Player - Central Multi-Game Autonomous Supervisor with Modern Web Dashboard
--------------------------------------------------------------------------------
Monitors and runs:
  1. Catizen: CATIZEN/play_catizen.js  (Telegram Topic ID: 2)
  2. Rignite: Rignite/rignite_tap_engine.py (Telegram Topic ID: 4)

Features:
- High-Performance Web Dashboard (http://localhost:8080):
    * Real-time metrics via auto-polling API (/api/status).
    * Glassmorphism dark UI with live cards, badges, energy meters, board layout.
    * Live streaming terminal log viewer with pause/filter controls.
    * Velocity analytics: Coins/sec, Gold/min, tap cadence, merge counts.
- Local Persistence:
    * Preserves full history in `logs/catizen_history.log` and `logs/rignite_history.log`.
    * Structured events audit in `logs/events_audit.jsonl`.
- Hourly Telegram + Telegraph Reports:
    * Detailed HTML report generated on Telegraph (Graph.org).
    * Overview summary posted to Telegram topic with preview disabled.
    * Startup alert sent to both topics on launch.
- Resilient Auto-Recovery:
    * Automatic restart with backoff if an engine exits or disconnects.
"""

import os
import sys
import time
import json
import signal
import asyncio
import requests
import subprocess
import threading
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional, List

from aiohttp import web
from pyrogram import Client, enums

# Ensure UTF-8 on Windows
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ==============================================================================
# CONFIGURATION & CONSTANTS
# ==============================================================================
API_ID = int(os.getenv("TELEGRAM_API_ID", "10471716"))
API_HASH = os.getenv("TELEGRAM_API_HASH", "f8a1b21a13af154596e2ff5bed164860")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8659110374:AAG1p0sSoGz3dxKEF6kkc2rV8ssJeNWgj7o")

TARGET_CHAT_ID = -1003959353966
TOPIC_CATIZEN = 2
TOPIC_RIGNITE = 4

REPORT_INTERVAL_SECONDS = 3600  # 1 Hour
WEB_PORT = int(os.getenv("WEB_PORT", os.getenv("PORT", "8080")))
PING_URL = os.getenv("PING_URL", os.getenv("RENDER_EXTERNAL_URL", ""))
PING_INTERVAL_SECONDS = int(os.getenv("PING_INTERVAL_SECONDS", "300"))  # Default 5 minutes

# IST Timezone: UTC + 5:30
IST_TZ = timezone(timedelta(hours=5, minutes=30))

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
LOGS_DIR = os.path.join(ROOT_DIR, "logs")
os.makedirs(LOGS_DIR, exist_ok=True)

CATIZEN_SCRIPT = os.path.join(ROOT_DIR, "CATIZEN", "play_catizen.js")
RIGNITE_SCRIPT = os.path.join(ROOT_DIR, "Rignite", "rignite_tap_engine.py")

CATIZEN_LOG_FILE = os.path.join(LOGS_DIR, "catizen_history.log")
RIGNITE_LOG_FILE = os.path.join(LOGS_DIR, "rignite_history.log")
EVENTS_AUDIT_FILE = os.path.join(LOGS_DIR, "events_audit.jsonl")

# ==============================================================================
# TELEGRAPH PUBLISHER (Graph.org / Telegra.ph compatible)
# ==============================================================================
class TelegraphManager:
    def __init__(self, author_name: str = "AutoPlayer Sentinel"):
        self.author_name = author_name
        self.access_token: Optional[str] = None
        self.api_base: str = "https://api.graph.org"
        self._init_account()

    def _init_account(self):
        endpoints = [
            "https://api.graph.org/createAccount",
            "https://api.telegra.ph/createAccount"
        ]
        for ep in endpoints:
            try:
                resp = requests.post(
                    ep,
                    json={"short_name": "AutoPlayer", "author_name": self.author_name},
                    timeout=10
                )
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("ok"):
                        self.access_token = data["result"]["access_token"]
                        self.api_base = ep.rsplit("/", 1)[0]
                        return
            except Exception:
                continue

    def create_page(self, title: str, html_nodes: List[Dict[str, Any]]) -> Optional[str]:
        if not self.access_token:
            self._init_account()
        if not self.access_token:
            return None

        endpoints = [
            f"{self.api_base}/createPage",
            "https://api.graph.org/createPage",
            "https://api.telegra.ph/createPage"
        ]

        payload = {
            "access_token": self.access_token,
            "title": title[:64],
            "author_name": self.author_name,
            "content": html_nodes,
            "return_content": False
        }

        for ep in endpoints:
            try:
                resp = requests.post(ep, json=payload, timeout=12)
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("ok"):
                        return data["result"]["url"]
            except Exception:
                continue
        return None


# ==============================================================================
# LOCAL FILE LOG LOGGER
# ==============================================================================
class FileLogger:
    @staticmethod
    def append_log(file_path: str, message: str):
        try:
            timestamp = datetime.now(IST_TZ).strftime("%Y-%m-%d %H:%M:%S")
            with open(file_path, "a", encoding="utf-8", errors="replace") as f:
                f.write(f"[{timestamp}] {message.strip()}\n")
        except Exception:
            pass

    @staticmethod
    def append_event(payload: Dict[str, Any]):
        try:
            with open(EVENTS_AUDIT_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except Exception:
            pass


# ==============================================================================
# METRICS & GAME TRACKER
# ==============================================================================
class GameTracker:
    def __init__(self, name: str, log_file: str):
        self.name = name
        self.log_file = log_file
        self.start_time = datetime.now(IST_TZ)
        self.period_start_time = self.start_time
        
        # State snapshot
        self.current_state: Dict[str, Any] = {}
        self.start_state: Dict[str, Any] = {}
        self.period_start_state: Dict[str, Any] = {}

        # Rolling raw text logs for web dashboard
        self.raw_logs: List[Dict[str, str]] = []

        # Structured events for hourly Telegraph report
        self.events_in_period: List[Dict[str, Any]] = []
        self.total_events: int = 0
        self.restart_count: int = 0
        self.status_pill: str = "INITIALIZING"
        self.is_enabled: bool = True
        self.last_heartbeat: float = time.time()
        self.proc_handle: Optional[subprocess.Popen] = None

    def add_log(self, text: str):
        self.last_heartbeat = time.time()
        cleaned = text.strip()
        if cleaned:
            now_str = datetime.now(IST_TZ).strftime("%H:%M:%S")
            self.raw_logs.append({"time": now_str, "text": cleaned})
            if len(self.raw_logs) > 60:
                self.raw_logs.pop(0)
            FileLogger.append_log(self.log_file, cleaned)

    def record_metric(self, payload: Dict[str, Any]):
        self.last_heartbeat = time.time()
        now = datetime.now(IST_TZ)
        self.total_events += 1
        self.status_pill = "ONLINE & ACTIVE"
        
        if not self.start_state:
            self.start_state = dict(payload)
        if not self.period_start_state:
            self.period_start_state = dict(payload)
            
        self.current_state = dict(payload)
        event_entry = {
            "time": now.strftime("%H:%M:%S"),
            "data": payload
        }
        self.events_in_period.append(event_entry)
        if len(self.events_in_period) > 300:
            self.events_in_period.pop(0)

        FileLogger.append_event({
            "game": self.name.lower(),
            "timestamp": now.isoformat(),
            **payload
        })

    def reset_period(self):
        self.period_start_time = datetime.now(IST_TZ)
        self.period_start_state = dict(self.current_state)
        self.events_in_period.clear()


# ==============================================================================
# HTML WEB DASHBOARD TEMPLATE (Embedded single-file with modern UI)
# ==============================================================================
WEB_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Auto-Player Web Command Dashboard</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;700&family=Plus+Jakarta+Sans:wght@400;600;700;800&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg: #0b0f19;
      --card-bg: rgba(18, 24, 38, 0.75);
      --card-border: rgba(255, 255, 255, 0.08);
      --text-main: #f1f5f9;
      --text-muted: #94a3b8;
      --accent-cat: #c084fc;
      --accent-cat-glow: rgba(192, 132, 252, 0.2);
      --accent-rig: #38bdf8;
      --accent-rig-glow: rgba(56, 189, 248, 0.2);
      --gold: #fbbf24;
      --green: #22c55e;
      --red: #ef4444;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      background: radial-gradient(circle at 50% 0%, #1e1b4b 0%, var(--bg) 70%);
      color: var(--text-main);
      font-family: 'Plus Jakarta Sans', sans-serif;
      min-height: 100vh;
      padding: 24px;
    }
    .header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      backdrop-filter: blur(12px);
      border-radius: 16px;
      padding: 18px 28px;
      margin-bottom: 24px;
    }
    .title-group h1 { font-size: 22px; font-weight: 800; letter-spacing: -0.5px; display: flex; align-items: center; gap: 10px; }
    .title-group p { font-size: 13px; color: var(--text-muted); margin-top: 4px; }
    .status-badge {
      display: inline-flex; align-items: center; gap: 8px; background: rgba(34, 197, 94, 0.15);
      color: var(--green); padding: 6px 14px; border-radius: 999px; font-size: 12px; font-weight: 700;
      border: 1px solid rgba(34, 197, 94, 0.3);
    }
    .status-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--green); box-shadow: 0 0 10px var(--green); }
    .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 24px; }
    @media (max-width: 980px) { .grid { grid-template-columns: 1fr; } }
    .game-card {
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 20px;
      padding: 24px;
      backdrop-filter: blur(14px);
      box-shadow: 0 12px 30px rgba(0,0,0,0.4);
      display: flex; flex-direction: column; gap: 20px;
    }
    .game-header { display: flex; justify-content: space-between; align-items: center; }
    .game-title { font-size: 18px; font-weight: 700; display: flex; align-items: center; gap: 8px; }
    .pill { font-size: 11px; font-weight: 700; padding: 4px 10px; border-radius: 6px; text-transform: uppercase; }
    .pill-cat { background: var(--accent-cat-glow); color: var(--accent-cat); border: 1px solid var(--accent-cat); }
    .pill-rig { background: var(--accent-rig-glow); color: var(--accent-rig); border: 1px solid var(--accent-rig); }
    .stats-row { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; }
    .stat-box { background: rgba(255,255,255,0.03); border: 1px solid rgba(255,255,255,0.05); border-radius: 12px; padding: 14px; }
    .stat-label { font-size: 12px; color: var(--text-muted); margin-bottom: 6px; }
    .stat-val { font-size: 19px; font-weight: 800; font-family: 'JetBrains Mono', monospace; }
    .cat-board {
      display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px;
      background: rgba(0,0,0,0.25); padding: 12px; border-radius: 12px; border: 1px solid var(--card-border);
    }
    .cat-slot {
      background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.08);
      border-radius: 8px; padding: 10px; text-align: center; font-family: 'JetBrains Mono', monospace;
      font-size: 12px; font-weight: 700; color: #cbd5e1;
    }
    .cat-slot.active { background: rgba(192, 132, 252, 0.15); border-color: var(--accent-cat); color: #fff; }
    .meter-bar-container { background: rgba(255,255,255,0.08); border-radius: 999px; height: 10px; overflow: hidden; margin-top: 6px; }
    .meter-bar-fill { height: 100%; border-radius: 999px; transition: width 0.3s ease; }
    .fill-green { background: linear-gradient(90deg, #22c55e, #4ade80); }
    .fill-cyan { background: linear-gradient(90deg, #0284c7, #38bdf8); }
    .log-box {
      background: #05070d; border: 1px solid rgba(255,255,255,0.06); border-radius: 12px;
      padding: 14px; height: 210px; overflow-y: auto; font-family: 'JetBrains Mono', monospace;
      font-size: 12px; line-height: 1.6; color: #94a3b8;
    }
    .log-line { display: flex; gap: 8px; margin-bottom: 4px; word-break: break-all; }
    .log-time { color: #64748b; font-size: 11px; min-width: 60px; }
    .btn-action {
      cursor: pointer; border: none; outline: none; border-radius: 8px; font-weight: 700;
      font-size: 12px; padding: 6px 14px; transition: all 0.2s ease; display: inline-flex; align-items: center; gap: 6px;
    }
    .btn-stop { background: rgba(239, 68, 68, 0.2); color: #f87171; border: 1px solid rgba(239, 68, 68, 0.4); }
    .btn-stop:hover { background: rgba(239, 68, 68, 0.4); color: #fff; }
    .btn-resume { background: rgba(34, 197, 94, 0.2); color: #4ade80; border: 1px solid rgba(34, 197, 94, 0.4); }
    .btn-resume:hover { background: rgba(34, 197, 94, 0.4); color: #fff; }
    .footer {
      margin-top: 24px; text-align: center; color: var(--text-muted); font-size: 12px;
      display: flex; justify-content: space-between; align-items: center;
    }
  </style>
</head>
<body>
  <div class="header">
    <div class="title-group">
      <h1><span>⚡</span> Auto-Player Live Sentinel Command</h1>
      <p>Autonomous Multi-Engine Automation • Asia/Kolkata (IST) • Local File Auditing Enabled</p>
    </div>
    <div style="display: flex; align-items: center; gap: 16px;">
      <div id="countdown" style="font-family: 'JetBrains Mono', monospace; font-size: 13px; color: var(--gold); background: rgba(251,191,36,0.1); padding: 8px 14px; border-radius: 8px; border: 1px solid rgba(251,191,36,0.2);">
        Next Hourly Report: --:--
      </div>
      <div class="status-badge"><span class="status-dot"></span> LIVE RUNNING</div>
    </div>
  </div>

  <div class="grid">
    <!-- CATIZEN CARD -->
    <div class="game-card">
      <div class="game-header">
        <div class="game-title">🐱 Catizen Automation <span class="pill pill-cat">Topic #2</span></div>
        <div style="display: flex; align-items: center; gap: 10px;">
          <button id="cat-btn" class="btn-action btn-stop" onclick="toggleGame('catizen')">🛑 Stop</button>
          <span id="cat-state" class="pill pill-cat">ONLINE</span>
        </div>
      </div>
      <div class="stats-row">
        <div class="stat-box">
          <div class="stat-label">Gold Balance</div>
          <div id="cat-gold" class="stat-val" style="color: var(--gold);">0</div>
        </div>
        <div class="stat-box">
          <div class="stat-label">Net Gold Delta</div>
          <div id="cat-delta" class="stat-val" style="color: var(--green);">+0</div>
        </div>
        <div class="stat-box">
          <div class="stat-label">Gold Velocity</div>
          <div id="cat-rate" class="stat-val" style="color: #cbd5e1;">0/s</div>
        </div>
      </div>
      <div class="stats-row">
        <div class="stat-box">
          <div class="stat-label">Max Record Cat</div>
          <div id="cat-max" class="stat-val" style="color: var(--accent-cat);">Lvl 0</div>
        </div>
        <div class="stat-box">
          <div class="stat-label">Board Top Cat</div>
          <div id="cat-top" class="stat-val" style="color: #fff;">Lvl 0</div>
        </div>
        <div class="stat-box">
          <div class="stat-label">Merges This Hour</div>
          <div id="cat-merges" class="stat-val" style="color: #38bdf8;">0</div>
        </div>
      </div>

      <div>
        <div style="display: flex; justify-content: space-between; font-size: 12px; margin-bottom: 8px; color: var(--text-muted);">
          <span>12-Slot Board Layout</span>
          <span id="cat-free-slots">12 Slots Free</span>
        </div>
        <div id="cat-board-grid" class="cat-board"></div>
      </div>

      <div>
        <div style="font-size: 12px; color: var(--text-muted); margin-bottom: 8px;">Live Engine Terminal Stream (Saved in logs/catizen_history.log)</div>
        <div id="cat-logs" class="log-box"></div>
      </div>
    </div>

    <!-- RIGNITE CARD -->
    <div class="game-card">
      <div class="game-header">
        <div class="game-title">⚡ Rignite Tap Engine <span class="pill pill-rig">Topic #4</span></div>
        <div style="display: flex; align-items: center; gap: 10px;">
          <button id="rig-btn" class="btn-action btn-stop" onclick="toggleGame('rignite')">🛑 Stop</button>
          <span id="rig-state" class="pill pill-rig">ONLINE</span>
        </div>
      </div>
      <div class="stats-row">
        <div class="stat-box">
          <div class="stat-label">Rignite Coins</div>
          <div id="rig-coins" class="stat-val" style="color: var(--gold);">0</div>
        </div>
        <div class="stat-box">
          <div class="stat-label">Net Coins Delta</div>
          <div id="rig-delta" class="stat-val" style="color: var(--green);">+0</div>
        </div>
        <div class="stat-box">
          <div class="stat-label">Coins Velocity (Rate)</div>
          <div id="rig-rate" class="stat-val" style="color: #cbd5e1;">0.0/s</div>
          <div id="rig-rate-cpm" style="font-size: 11px; color: var(--green); margin-top: 4px; font-weight: 700;">0 cpm</div>
        </div>
      </div>
      <div class="stats-row">
        <div class="stat-box">
          <div class="stat-label">Total Session Taps</div>
          <div id="rig-taps" class="stat-val" style="color: var(--accent-rig);">0</div>
          <div id="rig-tap-cadence" style="font-size: 11px; color: #94a3b8; margin-top: 4px;">0.0 taps/s</div>
        </div>
        <div class="stat-box">
          <div class="stat-label">Boosts & Ad Refills</div>
          <div id="rig-boosts" class="stat-val" style="color: #fff;">0</div>
          <div id="rig-ad-boosts" style="font-size: 11px; color: #38bdf8; margin-top: 4px;">Ads: 0 left</div>
        </div>
        <div class="stat-box">
          <div class="stat-label">Autonomous Cycle</div>
          <div id="rig-cycle" class="stat-val" style="color: #c084fc;">#1</div>
          <div id="rig-batches" style="font-size: 11px; color: #94a3b8; margin-top: 4px;">0 batches synced</div>
        </div>
      </div>

      <div>
        <div style="display: flex; justify-content: space-between; font-size: 12px; margin-bottom: 4px; color: var(--text-muted);">
          <span>Energy Tank Capacity</span>
          <span id="rig-energy-val">0 / 1000</span>
        </div>
        <div class="meter-bar-container"><div id="rig-energy-bar" class="meter-bar-fill fill-green" style="width: 0%;"></div></div>

        <div style="display: flex; justify-content: space-between; font-size: 12px; margin-top: 10px; margin-bottom: 4px; color: var(--text-muted);">
          <span>Battery Energy</span>
          <span id="rig-battery-val">0 / 0</span>
        </div>
        <div class="meter-bar-container"><div id="rig-battery-bar" class="meter-bar-fill fill-cyan" style="width: 0%;"></div></div>
      </div>

      <div>
        <div style="font-size: 12px; color: var(--text-muted); margin-bottom: 8px;">Live Engine Terminal Stream (Saved in logs/rignite_history.log)</div>
        <div id="rig-logs" class="log-box"></div>
      </div>
    </div>
  </div>

  <div class="footer">
    <div id="uptime">Uptime: 00m 00s</div>
    <div>Target Chat: -1003959353966 (Topics: 2 & 4) | Reports: Telegraph (Graph.org)</div>
    <div id="tg-status">Telegram Bot: Connecting...</div>
  </div>

  <script>
    async function updateDashboard() {
      try {
        const res = await fetch('/api/status');
        const data = await res.json();

        document.getElementById('countdown').innerText = `Next Hourly Report: ${data.countdown}`;
        document.getElementById('uptime').innerText = `Uptime: ${data.uptime}`;
        document.getElementById('tg-status').innerText = `Telegram Bot: ${data.tg_status}`;

        // Catizen Updates
        const cat = data.catizen;
        document.getElementById('cat-state').innerText = cat.status;
        const catBtn = document.getElementById('cat-btn');
        if (cat.enabled) {
          catBtn.className = 'btn-action btn-stop';
          catBtn.innerText = '🛑 Stop';
        } else {
          catBtn.className = 'btn-action btn-resume';
          catBtn.innerText = '▶️ Resume';
        }

        document.getElementById('cat-gold').innerText = cat.gold.toLocaleString();
        document.getElementById('cat-delta').innerText = (cat.gold_delta >= 0 ? '+' : '') + cat.gold_delta.toLocaleString();
        document.getElementById('cat-rate').innerText = `${cat.gold_rate.toLocaleString()}/s`;
        document.getElementById('cat-max').innerText = `Lvl ${cat.max_cat}`;
        document.getElementById('cat-top').innerText = `Lvl ${cat.highest_on_board}`;
        document.getElementById('cat-merges').innerText = cat.merges;
        document.getElementById('cat-free-slots').innerText = `${cat.empty_slots}/12 Slots Free`;

        // Render 12 Cat slots
        const grid = document.getElementById('cat-board-grid');
        grid.innerHTML = '';
        const board = cat.board.length === 12 ? cat.board : new Array(12).fill(0);
        board.forEach((lvl, idx) => {
          const div = document.createElement('div');
          div.className = 'cat-slot' + (lvl > 0 ? ' active' : '');
          div.innerText = lvl > 0 ? `Lvl ${lvl}` : `Slot ${idx}`;
          grid.appendChild(div);
        });

        // Cat Logs
        const catLogs = document.getElementById('cat-logs');
        catLogs.innerHTML = cat.logs.map(l => `<div class="log-line"><span class="log-time">${l.time}</span><span>${escapeHtml(l.text)}</span></div>`).join('');
        catLogs.scrollTop = catLogs.scrollHeight;

        // Rignite Updates
        const rig = data.rignite;
        document.getElementById('rig-state').innerText = rig.status;
        const rigBtn = document.getElementById('rig-btn');
        if (rig.enabled) {
          rigBtn.className = 'btn-action btn-stop';
          rigBtn.innerText = '🛑 Stop';
        } else {
          rigBtn.className = 'btn-action btn-resume';
          rigBtn.innerText = '▶️ Resume';
        }
        document.getElementById('rig-coins').innerText = rig.coins.toLocaleString();
        document.getElementById('rig-delta').innerText = (rig.coins_delta >= 0 ? '+' : '') + rig.coins_delta.toLocaleString();
        document.getElementById('rig-rate').innerText = `${rig.coins_rate.toFixed(1)}/s`;
        document.getElementById('rig-rate-cpm').innerText = `${rig.coins_cpm.toLocaleString()} coins/min`;
        document.getElementById('rig-taps').innerText = rig.session_taps.toLocaleString();
        document.getElementById('rig-tap-cadence').innerText = `${rig.tap_cadence.toFixed(1)} taps/s cadence`;
        document.getElementById('rig-boosts').innerText = `${rig.boosts} Instant`;
        document.getElementById('rig-ad-boosts').innerText = `Ad Refills: ${rig.ad_energy_left.toLocaleString()} left`;
        document.getElementById('rig-cycle').innerText = `#${rig.cycle}`;
        document.getElementById('rig-batches').innerText = `${rig.batches_synced} batches synced`;

        document.getElementById('rig-energy-val').innerText = `${rig.energy} / ${rig.max_energy}`;
        const ePct = Math.min(100, Math.max(0, (rig.energy / Math.max(1, rig.max_energy)) * 100));
        document.getElementById('rig-energy-bar').style.width = `${ePct}%`;

        document.getElementById('rig-battery-val').innerText = `${rig.battery_energy.toLocaleString()} / ${rig.battery_cap.toLocaleString()}`;
        const bPct = rig.battery_cap > 0 ? Math.min(100, Math.max(0, (rig.battery_energy / rig.battery_cap) * 100)) : 0;
        document.getElementById('rig-battery-bar').style.width = `${bPct}%`;

        // Rig Logs
        const rigLogs = document.getElementById('rig-logs');
        rigLogs.innerHTML = rig.logs.map(l => `<div class="log-line"><span class="log-time">${l.time}</span><span>${escapeHtml(l.text)}</span></div>`).join('');
        rigLogs.scrollTop = rigLogs.scrollHeight;

      } catch (err) {
        console.error('Update failed:', err);
      }
    }

    async function toggleGame(game) {
      try {
        const btn = document.getElementById(game === 'catizen' ? 'cat-btn' : 'rig-btn');
        const isStop = btn.innerText.includes('Stop');
        const action = isStop ? 'stop' : 'resume';
        btn.disabled = true;
        await fetch(`/api/control?game=${game}&action=${action}`, { method: 'POST' });
        setTimeout(() => { btn.disabled = false; updateDashboard(); }, 500);
      } catch (e) {
        console.error('Toggle error:', e);
      }
    }

    function escapeHtml(text) {
      return text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    }

    setInterval(updateDashboard, 1500);
    updateDashboard();
  </script>
</body>
</html>
"""

# ==============================================================================
# SUPERVISOR & PROCESS RUNNER
# ==============================================================================
class AutoPlayerSupervisor:
    def __init__(self):
        self.is_running = True
        self.start_time = datetime.now(IST_TZ)
        self.catizen_tracker = GameTracker("Catizen", CATIZEN_LOG_FILE)
        self.rignite_tracker = GameTracker("Rignite", RIGNITE_LOG_FILE)
        self.telegraph = TelegraphManager()
        self.loop = asyncio.new_event_loop()
        self.seconds_until_next_report = REPORT_INTERVAL_SECONDS
        self.last_tg_status: str = "Connecting..."
        
        self.client = Client(
            "autoplayer_session",
            api_id=API_ID,
            api_hash=API_HASH,
            bot_token=BOT_TOKEN,
            in_memory=True
        )

    def start_process(self, cmd: List[str], cwd: str, tracker: GameTracker, label: str):
        """Runs child process, routes output to web dashboard & local file, and parses metrics."""
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"

        while self.is_running:
            if not tracker.is_enabled:
                tracker.status_pill = "PAUSED BY USER"
                time.sleep(1)
                continue

            try:
                proc = subprocess.Popen(
                    cmd,
                    cwd=cwd,
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    universal_newlines=True,
                    encoding="utf-8",
                    errors="replace"
                )
                tracker.proc_handle = proc
                tracker.last_heartbeat = time.time()
                tracker.status_pill = "ONLINE & ACTIVE"

                for line in proc.stdout:
                    if not self.is_running or not tracker.is_enabled:
                        break
                    
                    # Intercept structured metric JSON
                    if "[AUTOPLAYER_METRIC]" in line:
                        try:
                            idx = line.find("[AUTOPLAYER_METRIC]")
                            json_str = line[idx + len("[AUTOPLAYER_METRIC]"):].strip()
                            data = json.loads(json_str)
                            tracker.record_metric(data)
                        except Exception:
                            pass
                        continue

                    # Record line in local history file and rolling buffer
                    tracker.add_log(line)
                    # Also print clean summary to console
                    sys.stdout.write(f"[{label}] {line}")
                    sys.stdout.flush()

                try:
                    proc.terminate()
                except Exception:
                    pass
                proc.wait()
            except Exception as e:
                tracker.add_log(f"Error: {e}")

            tracker.proc_handle = None

            if not self.is_running:
                break

            if not tracker.is_enabled:
                tracker.status_pill = "PAUSED BY USER"
                tracker.add_log(f"⏸️ Game autoplay paused by dashboard control.")
                while not tracker.is_enabled and self.is_running:
                    time.sleep(1)
                if self.is_running:
                    tracker.add_log(f"▶️ Game autoplay resumed. Launching engine...")
                continue

            tracker.restart_count += 1
            tracker.status_pill = f"RECONNECTING #{tracker.restart_count}"
            tracker.add_log(f"⚠️ Process stopped. Auto-restarting in 5s (Restart #{tracker.restart_count})...")
            time.sleep(5)

    # --------------------------------------------------------------------------
    # API STATUS SERIALIZER FOR WEB DASHBOARD
    # --------------------------------------------------------------------------
    def get_status_payload(self) -> Dict[str, Any]:
        now = datetime.now(IST_TZ)
        mins_left = max(0, self.seconds_until_next_report // 60)
        secs_left = max(0, self.seconds_until_next_report % 60)
        countdown_str = f"{mins_left:02d}m {secs_left:02d}s"

        session_sec = max(1.0, (now - self.start_time).total_seconds())
        el_h = int(session_sec // 3600)
        el_m = int((session_sec % 3600) // 60)
        el_s = int(session_sec % 60)
        uptime_fmt = f"{el_h}h {el_m:02d}m {el_s:02d}s"

        # Catizen stats
        c_state = self.catizen_tracker.current_state
        c_init = self.catizen_tracker.start_state
        c_curr_gold = c_state.get("gold", 0)
        c_start_gold = c_init.get("gold", c_curr_gold)
        c_gold_delta = c_curr_gold - c_start_gold
        c_gold_rate = max(0, c_gold_delta) / session_sec

        c_merges = sum(1 for e in self.catizen_tracker.events_in_period if e["data"].get("type") == "merge")
        c_buys = sum(1 for e in self.catizen_tracker.events_in_period if e["data"].get("type") == "buy_cat")

        # Rignite stats
        r_state = self.rignite_tracker.current_state
        r_init = self.rignite_tracker.start_state
        r_curr_coins = r_state.get("coins", 0)
        r_start_coins = r_init.get("coins", r_curr_coins)
        r_coins_delta = r_curr_coins - r_start_coins
        r_coins_rate = max(0, r_coins_delta) / session_sec
        r_coins_cpm = int(r_coins_rate * 60.0)
        r_taps = r_state.get("session_taps", 0)
        r_cadence = (r_taps / session_sec) if session_sec > 0 else 0.0
        r_batches = sum(1 for e in self.rignite_tracker.events_in_period if e["data"].get("type") == "tap_sync")

        return {
            "uptime": uptime_fmt,
            "countdown": countdown_str,
            "tg_status": self.last_tg_status,
            "catizen": {
                "status": self.catizen_tracker.status_pill,
                "enabled": self.catizen_tracker.is_enabled,
                "gold": c_curr_gold,
                "gold_delta": c_gold_delta,
                "gold_rate": int(c_gold_rate),
                "max_cat": c_state.get("max_cat_lvl", 0),
                "highest_on_board": c_state.get("highest_on_board", 0),
                "empty_slots": c_state.get("empty_slots", 0),
                "merges": c_merges,
                "buys": c_buys,
                "board": c_state.get("board", []),
                "logs": self.catizen_tracker.raw_logs[-35:]
            },
            "rignite": {
                "status": self.rignite_tracker.status_pill,
                "enabled": self.rignite_tracker.is_enabled,
                "coins": r_curr_coins,
                "coins_delta": r_coins_delta,
                "coins_rate": r_coins_rate,
                "coins_cpm": r_coins_cpm,
                "energy": r_state.get("energy", 0),
                "max_energy": r_state.get("max_energy", 1000),
                "battery_energy": r_state.get("battery_energy", 0),
                "battery_cap": r_state.get("battery_cap", 0),
                "session_taps": r_taps,
                "tap_cadence": r_cadence,
                "boosts": r_state.get("full_energy_left", 0),
                "ad_energy_left": r_state.get("ad_energy_left", 0),
                "batches_synced": r_batches,
                "cycle": r_state.get("cycle_count", 1),
                "logs": self.rignite_tracker.raw_logs[-35:]
            }
        }

    # --------------------------------------------------------------------------
    # WEB APPLICATION SERVER
    # --------------------------------------------------------------------------
    async def init_web_app(self) -> web.AppRunner:
        app = web.Application()
        
        async def handle_index(request):
            return web.Response(text=WEB_DASHBOARD_HTML, content_type="text/html")

        async def handle_status(request):
            return web.json_response(self.get_status_payload())

        async def handle_control(request):
            try:
                game = request.query.get("game", "").lower()
                action = request.query.get("action", "").lower()

                target = None
                if game == "catizen":
                    target = self.catizen_tracker
                elif game == "rignite":
                    target = self.rignite_tracker

                if not target:
                    return web.json_response({"ok": False, "error": "Unknown game"}, status=400)

                if action == "stop":
                    target.is_enabled = False
                    if target.proc_handle:
                        try:
                            target.proc_handle.terminate()
                        except Exception:
                            pass
                    target.status_pill = "PAUSED BY USER"
                    target.add_log(f"🛑 [CONTROL] Autoplay stopped via dashboard.")
                    return web.json_response({"ok": True, "status": "stopped", "game": game})
                elif action == "resume":
                    target.is_enabled = True
                    target.status_pill = "STARTING..."
                    target.add_log(f"▶️ [CONTROL] Autoplay resumed via dashboard.")
                    return web.json_response({"ok": True, "status": "resumed", "game": game})
                else:
                    return web.json_response({"ok": False, "error": "Invalid action"}, status=400)
            except Exception as e:
                return web.json_response({"ok": False, "error": str(e)}, status=500)

        app.router.add_get("/", handle_index)
        app.router.add_get("/api/status", handle_status)
        app.router.add_post("/api/control", handle_control)
        app.router.add_get("/api/control", handle_control)
        app.router.add_get("/ping", lambda req: web.Response(text="pong"))
        app.router.add_get("/healthz", lambda req: web.Response(text="ok"))

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", WEB_PORT)
        await site.start()
        print(f"🌐 [Web Dashboard Active] Open http://localhost:{WEB_PORT} in your browser")
        return runner

    # --------------------------------------------------------------------------
    # TELEGRAM REPORT DISPATCHER
    # --------------------------------------------------------------------------
    async def send_hourly_reports(self):
        now_ist = datetime.now(IST_TZ)
        cat_time_from = self.catizen_tracker.period_start_time.strftime("%I:%M %p")
        cat_time_to = now_ist.strftime("%I:%M %p IST")
        date_str = now_ist.strftime("%d %b %Y")

        # ---------------- CATIZEN REPORT ----------------
        try:
            cat_url = self.generate_catizen_telegraph(date_str, cat_time_from, cat_time_to)
            cat_text = self.build_catizen_tg_message(date_str, cat_time_from, cat_time_to, cat_url)
            await self.client.send_message(
                chat_id=TARGET_CHAT_ID,
                message_thread_id=TOPIC_CATIZEN,
                text=cat_text,
                parse_mode=enums.ParseMode.HTML,
                disable_web_page_preview=True
            )
            self.catizen_tracker.reset_period()
        except Exception as e:
            self.catizen_tracker.add_log(f"Telegram dispatch error: {e}")

        # ---------------- RIGNITE REPORT ----------------
        try:
            rig_url = self.generate_rignite_telegraph(date_str, cat_time_from, cat_time_to)
            rig_text = self.build_rignite_tg_message(date_str, cat_time_from, cat_time_to, rig_url)
            await self.client.send_message(
                chat_id=TARGET_CHAT_ID,
                message_thread_id=TOPIC_RIGNITE,
                text=rig_text,
                parse_mode=enums.ParseMode.HTML,
                disable_web_page_preview=True
            )
            self.rignite_tracker.reset_period()
        except Exception as e:
            self.rignite_tracker.add_log(f"Telegram dispatch error: {e}")

    # --------------------------------------------------------------------------
    # CATIZEN MESSAGES & TELEGRAPH BUILDERS
    # --------------------------------------------------------------------------
    def build_catizen_tg_message(self, date_str: str, time_from: str, time_to: str, telegraph_url: Optional[str]) -> str:
        curr = self.catizen_tracker.current_state
        prev = self.catizen_tracker.period_start_state

        curr_gold = curr.get("gold", 0)
        prev_gold = prev.get("gold", curr_gold)
        gold_diff = curr_gold - prev_gold

        max_cat = curr.get("max_cat_lvl", 0)
        highest_on_board = curr.get("highest_on_board", 0)
        empty_slots = curr.get("empty_slots", 0)
        user_name = curr.get("user_name") or curr.get("nick_name") or "Commander"
        restarts = self.catizen_tracker.restart_count

        merges = sum(1 for e in self.catizen_tracker.events_in_period if e["data"].get("type") == "merge")
        buys = sum(1 for e in self.catizen_tracker.events_in_period if e["data"].get("type") == "buy_cat")
        airdrops = sum(1 for e in self.catizen_tracker.events_in_period if e["data"].get("type") == "airdrop_claimed")

        delta_sign = "+" if gold_diff >= 0 else ""
        link_line = f"\n🌐 <b>Full Hourly Audit:</b> <a href=\"{telegraph_url}\">View Detailed Telegraph Report</a>\n" if telegraph_url else ""

        return (
            f"🐱 <b>CATIZEN AUTOMATION OVERVIEW</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📅 <b>Date:</b> {date_str}\n"
            f"⏰ <b>Window:</b> <code>{time_from}</code> ➔ <code>{time_to}</code>\n"
            f"👤 <b>Player:</b> <b>{user_name}</b>\n"
            f"🟢 <b>Status:</b> <code>ONLINE & MERGING</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"💰 <b>Gold Balance:</b> <code>{curr_gold:,}</code>\n"
            f"📈 <b>Net Gold Delta:</b> <code>{delta_sign}{gold_diff:,}</code>\n"
            f"🏆 <b>Highest Cat Record:</b> <b>Lvl {max_cat}</b>\n"
            f"🐾 <b>Top Cat on Board:</b> <b>Lvl {highest_on_board}</b>\n"
            f"📦 <b>Free Slots:</b> <code>{empty_slots}/12</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"⚡ <b>1-Hour Performance:</b>\n"
            f"  • Merges Completed: <b>{merges}</b>\n"
            f"  • Top Cats Purchased: <b>{buys}</b>\n"
            f"  • Airdrops Claimed: <b>{airdrops}</b>\n"
            f"  • Engine Restarts: <b>{restarts}</b>\n"
            f"{link_line}"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"<i>Auto-Player Sentinel • Asia/Kolkata (IST)</i>"
        )

    def generate_catizen_telegraph(self, date_str: str, time_from: str, time_to: str) -> Optional[str]:
        curr = self.catizen_tracker.current_state
        prev = self.catizen_tracker.period_start_state
        curr_gold = curr.get("gold", 0)
        prev_gold = prev.get("gold", curr_gold)
        gold_diff = curr_gold - prev_gold

        board = curr.get("board", [])
        board_str = ", ".join(f"Slot {i}: Lvl {lvl}" for i, lvl in enumerate(board)) if board else "N/A"

        recent_events = self.catizen_tracker.events_in_period[-40:]
        event_nodes = []
        for ev in reversed(recent_events):
            t = ev.get("time", "")
            d = ev.get("data", {})
            ev_type = d.get("type", "event")
            event_nodes.append({
                "tag": "li",
                "children": [f"[{t}] Type: {ev_type} | Gold: {d.get('gold', 0):,} | Top: Lvl {d.get('highest_on_board', 0)}"]
            })

        if not event_nodes:
            event_nodes.append({"tag": "li", "children": ["No major events recorded in this hourly slice."]})

        nodes = [
            {"tag": "h3", "children": [f"Catizen Engine Detailed Status ({date_str})"]},
            {"tag": "p", "children": [f"Interval: {time_from} to {time_to} (IST)"]},
            {"tag": "h4", "children": ["Financial & Progression Snapshot"]},
            {
                "tag": "ul",
                "children": [
                    {"tag": "li", "children": [f"Start Gold: {prev_gold:,}"]},
                    {"tag": "li", "children": [f"Current Gold: {curr_gold:,} (Net: {'+' if gold_diff >= 0 else ''}{gold_diff:,})"]},
                    {"tag": "li", "children": [f"Max Cat Level Record: Lvl {curr.get('max_cat_lvl', 0)}"]},
                    {"tag": "li", "children": [f"Current Board Layout: {board_str}"]},
                    {"tag": "li", "children": [f"Supervisor Restarts: {self.catizen_tracker.restart_count}"]}
                ]
            },
            {"tag": "h4", "children": ["Chronological Activity Log (Last 40 Events)"]},
            {"tag": "ul", "children": event_nodes}
        ]

        return self.telegraph.create_page(
            title=f"Catizen Hourly Report - {date_str} {time_to}",
            html_nodes=nodes
        )

    # --------------------------------------------------------------------------
    # RIGNITE MESSAGES & TELEGRAPH BUILDERS
    # --------------------------------------------------------------------------
    def build_rignite_tg_message(self, date_str: str, time_from: str, time_to: str, telegraph_url: Optional[str]) -> str:
        curr = self.rignite_tracker.current_state
        prev = self.rignite_tracker.period_start_state

        curr_coins = curr.get("coins", 0)
        prev_coins = prev.get("coins", curr_coins)
        coins_diff = curr_coins - prev_coins

        user_name = curr.get("user", "Player")
        energy = curr.get("energy", 0)
        max_energy = curr.get("max_energy", 1000)
        full_energy_left = curr.get("full_energy_left", 0)
        session_taps = curr.get("session_taps", 0)
        cycle_count = curr.get("cycle_count", 1)
        restarts = self.rignite_tracker.restart_count

        syncs = sum(1 for e in self.rignite_tracker.events_in_period if e["data"].get("type") == "tap_sync")
        refills = sum(1 for e in self.rignite_tracker.events_in_period if e["data"].get("type") == "boost_refill")

        delta_sign = "+" if coins_diff >= 0 else ""
        link_line = f"\n🌐 <b>Full Hourly Audit:</b> <a href=\"{telegraph_url}\">View Detailed Telegraph Report</a>\n" if telegraph_url else ""

        return (
            f"⚡ <b>RIGNITE TAP ENGINE OVERVIEW</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📅 <b>Date:</b> {date_str}\n"
            f"⏰ <b>Window:</b> <code>{time_from}</code> ➔ <code>{time_to}</code>\n"
            f"👤 <b>Player:</b> <b>@{user_name}</b>\n"
            f"🟢 <b>Status:</b> <code>TAP-BURSTING & CYCLING</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🪙 <b>Rignite Coins:</b> <code>{curr_coins:,}</code>\n"
            f"📈 <b>Net Coins Delta:</b> <code>{delta_sign}{coins_diff:,}</code>\n"
            f"🔋 <b>Energy Reserve:</b> <code>{energy}/{max_energy}</code>\n"
            f"⚡ <b>Boost Refills Remaining:</b> <code>{full_energy_left}</code>\n"
            f"🔄 <b>Autonomous Cycle:</b> <code>#{cycle_count}</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🚀 <b>1-Hour Performance:</b>\n"
            f"  • Tap Batches Synced: <b>{syncs}</b>\n"
            f"  • Total Taps Dispatched: <b>{session_taps:,}</b>\n"
            f"  • Instant Boosts Used: <b>{refills}</b>\n"
            f"  • Engine Restarts: <b>{restarts}</b>\n"
            f"{link_line}"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"<i>Auto-Player Sentinel • Asia/Kolkata (IST)</i>"
        )

    def generate_rignite_telegraph(self, date_str: str, time_from: str, time_to: str) -> Optional[str]:
        curr = self.rignite_tracker.current_state
        prev = self.rignite_tracker.period_start_state
        curr_coins = curr.get("coins", 0)
        prev_coins = prev.get("coins", curr_coins)
        coins_diff = curr_coins - prev_coins

        recent_events = self.rignite_tracker.events_in_period[-40:]
        event_nodes = []
        for ev in reversed(recent_events):
            t = ev.get("time", "")
            d = ev.get("data", {})
            ev_type = d.get("type", "event")
            event_nodes.append({
                "tag": "li",
                "children": [f"[{t}] Type: {ev_type} | Coins: {d.get('coins', 0):,} | Energy: {d.get('energy', 0)}/{d.get('max_energy', 1000)} | Taps: {d.get('session_taps', 0):,}"]
            })

        if not event_nodes:
            event_nodes.append({"tag": "li", "children": ["No major tap events recorded in this hourly slice."]})

        nodes = [
            {"tag": "h3", "children": [f"Rignite Engine Detailed Status ({date_str})"]},
            {"tag": "p", "children": [f"Interval: {time_from} to {time_to} (IST)"]},
            {"tag": "h4", "children": ["Balance & Telemetry Snapshot"]},
            {
                "tag": "ul",
                "children": [
                    {"tag": "li", "children": [f"Player Account: @{curr.get('user', 'Player')}"]},
                    {"tag": "li", "children": [f"Start Coins: {prev_coins:,}"]},
                    {"tag": "li", "children": [f"Current Coins: {curr_coins:,} (Net: {'+' if coins_diff >= 0 else ''}{coins_diff:,})"]},
                    {"tag": "li", "children": [f"Energy: {curr.get('energy', 0)}/{curr.get('max_energy', 1000)}"]},
                    {"tag": "li", "children": [f"Battery Energy: {curr.get('battery_energy', 0)}/{curr.get('battery_cap', 0)}"]},
                    {"tag": "li", "children": [f"Total Session Taps: {curr.get('session_taps', 0):,}"]},
                    {"tag": "li", "children": [f"Cycle Count: #{curr.get('cycle_count', 1)}"]},
                    {"tag": "li", "children": [f"Supervisor Restarts: {self.rignite_tracker.restart_count}"]}
                ]
            },
            {"tag": "h4", "children": ["Chronological Activity Log (Last 40 Batches)"]},
            {"tag": "ul", "children": event_nodes}
        ]

        return self.telegraph.create_page(
            title=f"Rignite Hourly Report - {date_str} {time_to}",
            html_nodes=nodes
        )

    # --------------------------------------------------------------------------
    # STARTUP NOTIFICATION
    # --------------------------------------------------------------------------
    async def send_startup_messages(self):
        """Sends an initialization announcement to both topics upon launch."""
        now_ist = datetime.now(IST_TZ).strftime("%d %b %Y, %I:%M:%S %p IST")

        catizen_msg = (
            f"🐱 <b>CATIZEN ENGINE ONLINE</b> 🟢\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🚀 <b>Auto-Player Supervisor:</b> Initialized\n"
            f"🕒 <b>Start Time:</b> <code>{now_ist}</code>\n"
            f"⚙️ <b>Engine Target:</b> <code>CATIZEN/play_catizen.js</code>\n"
            f"⏱️ <b>Hourly Audit Window:</b> Every 60 Minutes\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"<i>Autonomous Catizen gameplay & telemetry tracking active.</i>"
        )

        rignite_msg = (
            f"⚡ <b>RIGNITE TAP ENGINE ONLINE</b> 🟢\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🚀 <b>Auto-Player Supervisor:</b> Initialized\n"
            f"🕒 <b>Start Time:</b> <code>{now_ist}</code>\n"
            f"⚙️ <b>Engine Target:</b> <code>Rignite/rignite_tap_engine.py</code>\n"
            f"⏱️ <b>Hourly Audit Window:</b> Every 60 Minutes\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"<i>Dynamic tap-bursting & human telemetry tracking active.</i>"
        )

        try:
            await self.client.send_message(
                chat_id=TARGET_CHAT_ID,
                message_thread_id=TOPIC_CATIZEN,
                text=catizen_msg,
                parse_mode=enums.ParseMode.HTML,
                disable_web_page_preview=True
            )
        except Exception as e:
            self.catizen_tracker.add_log(f"Catizen startup alert error: {e}")

        try:
            await self.client.send_message(
                chat_id=TARGET_CHAT_ID,
                message_thread_id=TOPIC_RIGNITE,
                text=rignite_msg,
                parse_mode=enums.ParseMode.HTML,
                disable_web_page_preview=True
            )
        except Exception as e:
            self.rignite_tracker.add_log(f"Rignite startup alert error: {e}")

    # --------------------------------------------------------------------------
    # BACKGROUND SCHEDULE RUNNER & WEB SERVER
    # --------------------------------------------------------------------------
    async def scheduler_loop(self):
        self.last_tg_status = "Connecting..."
        await self.client.start()
        self.last_tg_status = "Connected & Active"
        await self.send_startup_messages()

        # Start Web Dashboard
        web_runner = await self.init_web_app()

        # Self-Pinger Background Task (keeps Render / cloud containers awake)
        async def self_pinger_task():
            target_url = PING_URL.strip()
            if not target_url:
                return

            if not target_url.startswith("http://") and not target_url.startswith("https://"):
                target_url = f"https://{target_url}"

            print(f"📡 [Self-Pinger Active] Target: {target_url} (Every {PING_INTERVAL_SECONDS}s)")
            while self.is_running:
                try:
                    await asyncio.sleep(PING_INTERVAL_SECONDS)
                    if not self.is_running:
                        break
                    res = requests.get(target_url, timeout=15)
                    # Silently logged or in history
                    timestamp = datetime.now(IST_TZ).strftime("%H:%M:%S")
                    FileLogger.append_log(
                        os.path.join(LOGS_DIR, "pinger.log"),
                        f"Pinged {target_url} -> Status: {res.status_code}"
                    )
                except Exception as pe:
                    FileLogger.append_log(
                        os.path.join(LOGS_DIR, "pinger.log"),
                        f"Ping error ({target_url}): {pe}"
                    )

        asyncio.create_task(self_pinger_task())

        while self.is_running:
            try:
                for _ in range(REPORT_INTERVAL_SECONDS):
                    if not self.is_running:
                        break
                    self.seconds_until_next_report -= 1
                    await asyncio.sleep(1)

                if self.is_running:
                    self.seconds_until_next_report = REPORT_INTERVAL_SECONDS
                    await self.send_hourly_reports()
            except Exception as e:
                await asyncio.sleep(5)

        await web_runner.cleanup()
        await self.client.stop()

    # --------------------------------------------------------------------------
    # RESILIENCE & WATCHDOG MONITOR
    # --------------------------------------------------------------------------
    def watchdog_monitor_loop(self):
        """Monitors child processes for silence or deadlocks and forces recycling if unresponsive."""
        CATIZEN_TIMEOUT_SEC = 90
        RIGNITE_TIMEOUT_SEC = 240  # Accounts for 150s rest cooldowns

        while self.is_running:
            time.sleep(10)
            now = time.time()

            # Catizen silence watchdog
            if self.catizen_tracker.proc_handle and (now - self.catizen_tracker.last_heartbeat > CATIZEN_TIMEOUT_SEC):
                self.catizen_tracker.add_log(f"⚠️ [WATCHDOG] Catizen process silent > {CATIZEN_TIMEOUT_SEC}s. Forcing restart...")
                try:
                    self.catizen_tracker.proc_handle.terminate()
                except Exception:
                    pass
                self.catizen_tracker.last_heartbeat = now

            # Rignite silence watchdog
            if self.rignite_tracker.proc_handle and (now - self.rignite_tracker.last_heartbeat > RIGNITE_TIMEOUT_SEC):
                self.rignite_tracker.add_log(f"⚠️ [WATCHDOG] Rignite process silent > {RIGNITE_TIMEOUT_SEC}s. Forcing restart...")
                try:
                    self.rignite_tracker.proc_handle.terminate()
                except Exception:
                    pass
                self.rignite_tracker.last_heartbeat = now

    def run(self):
        print("=" * 65)
        print("🚀 Auto-Player Central Supervisor Starting")
        print(f"🕒 Timezone: Asia/Kolkata (IST)")
        print(f"📢 Telegram Chat: {TARGET_CHAT_ID} (Topics: 2 & 4)")
        print(f"🌐 Web Dashboard: http://localhost:{WEB_PORT}")
        print(f"📁 Local Log Storage: logs/")
        print("=" * 65)

        # 1. Start Catizen Engine Thread
        cat_cmd = ["node", CATIZEN_SCRIPT]
        cat_thread = threading.Thread(
            target=self.start_process,
            args=(cat_cmd, os.path.dirname(CATIZEN_SCRIPT), self.catizen_tracker, "Catizen"),
            daemon=True
        )
        cat_thread.start()

        # 2. Start Rignite Engine Thread
        rig_cmd = [sys.executable, "-u", RIGNITE_SCRIPT]
        rig_thread = threading.Thread(
            target=self.start_process,
            args=(rig_cmd, os.path.dirname(RIGNITE_SCRIPT), self.rignite_tracker, "Rignite"),
            daemon=True
        )
        rig_thread.start()

        # 3. Start Heartbeat Watchdog Thread
        watchdog_thread = threading.Thread(target=self.watchdog_monitor_loop, daemon=True)
        watchdog_thread.start()

        # 3. Handle clean shutdown signal
        def sig_handler(sig, frame):
            print("\n[Supervisor] Shutting down cleanly...")
            self.is_running = False

        signal.signal(signal.SIGINT, sig_handler)
        signal.signal(signal.SIGTERM, sig_handler)

        # 4. Run Pyrogram Async Scheduler & Web Server
        try:
            self.loop.run_until_complete(self.scheduler_loop())
        except KeyboardInterrupt:
            pass
        finally:
            self.is_running = False
            print("[Supervisor] Auto-Player stopped cleanly.")


if __name__ == "__main__":
    supervisor = AutoPlayerSupervisor()
    supervisor.run()
