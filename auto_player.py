"""
Auto Player - Central Multi-Game Autonomous Supervisor with Modern Web Dashboard
--------------------------------------------------------------------------------
Monitors and runs:
  1. Catizen: CATIZEN/play_catizen.js  (Telegram Topic ID: 2)
    2. Rignite: Rignite/rignite_tap_engine.py (Telegram Topic ID: 4)
  3. Cats vs Zombies: CatVSZombie/cat_vs_zombie_engine.py (Telegram Topic ID: 6)

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
from pyrogram.errors import FloodWait, RPCError

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
TOPIC_CATVSZOMBIE = int(os.getenv("TOPIC_CATVSZOMBIE", "6"))

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
CATVSZOMBIE_SCRIPT = os.path.join(ROOT_DIR, "CatVSZombie", "cat_vs_zombie_engine.py")

CATIZEN_LOG_FILE = os.path.join(LOGS_DIR, "catizen_history.log")
RIGNITE_LOG_FILE = os.path.join(LOGS_DIR, "rignite_history.log")
CATVSZOMBIE_LOG_FILE = os.path.join(LOGS_DIR, "catvszombie_history.log")
CATVSZOMBIE_CMD_FILE = os.path.join(LOGS_DIR, "catvszombie_cmd.json")
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

    def reset_metrics_baseline(self):
        """Resets baseline states and starts fresh so spent coins don't distort session metrics."""
        self.start_time = datetime.now(IST_TZ)
        if self.current_state:
            self.start_state = dict(self.current_state)
            self.period_start_state = dict(self.current_state)
            # Reset current delta / session tap counts
            self.current_state["session_taps"] = 0
            self.current_state["coins_delta"] = 0
            self.current_state["start_coins"] = self.current_state.get("coins", 0)
        else:
            self.start_state = {}
            self.period_start_state = {}
        self.events_in_period.clear()

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
            
        # Preserve sticky attributes like web_app_url across metrics updates
        prev_url = self.current_state.get("web_app_url", "")
        self.current_state = dict(payload)
        if not self.current_state.get("web_app_url") and prev_url:
            self.current_state["web_app_url"] = prev_url
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
      --accent-cvz: #10b981;
      --accent-cvz-glow: rgba(16, 185, 129, 0.2);
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
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(380px, 1fr)); gap: 24px; }
    @media (max-width: 980px) { .grid { grid-template-columns: 1fr; } }
    .pill-cvz { background: var(--accent-cvz-glow); color: var(--accent-cvz); border: 1px solid var(--accent-cvz); }
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
    .btn-open { background: rgba(56, 189, 248, 0.18); color: #38bdf8; border: 1px solid rgba(56, 189, 248, 0.4); text-decoration: none; }
    .btn-open:hover { background: rgba(56, 189, 248, 0.35); color: #fff; }
    .btn-stop { background: rgba(239, 68, 68, 0.2); color: #f87171; border: 1px solid rgba(239, 68, 68, 0.4); }
    .btn-stop:hover { background: rgba(239, 68, 68, 0.4); color: #fff; }
    .btn-resume { background: rgba(34, 197, 94, 0.2); color: #4ade80; border: 1px solid rgba(34, 197, 94, 0.4); }
    .btn-resume:hover { background: rgba(34, 197, 94, 0.4); color: #fff; }
    .btn-ad-refill { background: rgba(251, 191, 36, 0.2); color: #fbbf24; border: 1px solid rgba(251, 191, 36, 0.4); }
    .btn-ad-refill:hover { background: rgba(251, 191, 36, 0.4); color: #fff; }
    .btn-ad-refill:disabled { opacity: 0.5; cursor: not-allowed; }
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
        <div style="display: flex; align-items: center; gap: 8px;">
          <a id="cat-open-btn" class="btn-action btn-open" href="#" target="_blank" style="display: none;">🎮 Open Game</a>
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
        <div style="display: flex; align-items: center; gap: 8px;">
          <a id="rig-open-btn" class="btn-action btn-open" href="#" target="_blank" style="display: none;">🎮 Open Game</a>
          <button id="rig-ad-btn" class="btn-action btn-ad-refill" onclick="triggerManualAdRefill()" title="Watch Rewarded Ad to Refill Tank Energy">🎬 Watch Ad Refill</button>
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
          <div class="stat-label">Colony Passive Coins</div>
          <div id="rig-colony-coins" class="stat-val" style="color: #38bdf8;">0</div>
          <div style="font-size: 11px; color: #94a3b8; margin-top: 4px;">Excluded from Tap CPM</div>
        </div>
        <div class="stat-box">
          <div class="stat-label">Boosts & Ad Refills</div>
          <div id="rig-boosts" class="stat-val" style="color: #fff;">0</div>
          <div id="rig-ad-boosts" style="font-size: 11px; color: #38bdf8; margin-top: 4px;">Ads: 0 left</div>
        </div>
      </div>

      <!-- Action & Refill Status Banner -->
      <div style="background: rgba(255,255,255,0.03); border: 1px solid rgba(255,255,255,0.07); border-radius: 12px; padding: 12px 16px; display: flex; flex-direction: column; gap: 6px;">
        <div style="display: flex; justify-content: space-between; align-items: center;">
          <span style="font-size: 11px; color: var(--text-muted); text-transform: uppercase; font-weight: 700; letter-spacing: 0.5px;">Refill & Engine Action</span>
          <div style="display: flex; align-items: center; gap: 8px;">
            <span id="rig-batches" style="font-size: 11px; color: #94a3b8; font-family: 'JetBrains Mono', monospace;">0 batches</span>
            <span id="rig-cycle" class="pill" style="background: rgba(251, 191, 36, 0.15); color: var(--gold); border: 1px solid rgba(251, 191, 36, 0.3); font-size: 11px; padding: 2px 8px;">Cycle #1</span>
            <span id="rig-action-badge" class="pill" style="background: rgba(56, 189, 248, 0.15); color: #38bdf8; border: 1px solid rgba(56, 189, 248, 0.3);">ACTIVE TAPPING</span>
          </div>
        </div>
        <div style="display: flex; justify-content: space-between; align-items: baseline; font-size: 13px;">
          <span id="rig-action-status" style="font-weight: 600; color: #f1f5f9;">Engine operating normally</span>
          <span id="rig-next-action" style="font-size: 12px; color: var(--gold); font-family: 'JetBrains Mono', monospace;">Ready</span>
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
    <!-- CATS VS ZOMBIES CARD -->
    <div class="game-card">
      <div class="game-header">
        <div class="game-title">🧟 Cats vs Zombies <span class="pill pill-cvz">Topic #6</span></div>
        <div style="display: flex; align-items: center; gap: 8px;">
          <a id="cvz-open-btn" class="btn-action btn-open" href="#" target="_blank" style="display: none;">🎮 Open Game</a>
          <button id="cvz-repair-btn" class="btn-action btn-ad-refill" onclick="triggerCvzAction('repair_wall')" title="Manually Trigger Fortress Wall Repair">🛠️ Repair Wall</button>
          <button id="cvz-claim-btn" class="btn-action btn-ad-refill" onclick="triggerCvzAction('claim_tasks')" title="Claim Daily Tasks & Milestones">🎁 Claim Daily</button>
          <button id="cvz-btn" class="btn-action btn-stop" onclick="toggleGame('catvszombie')">🛑 Stop</button>
          <span id="cvz-state" class="pill pill-cvz">ONLINE</span>
        </div>
      </div>
      <div class="stats-row">
        <div class="stat-box">
          <div class="stat-label">Gold Balance</div>
          <div id="cvz-gold" class="stat-val" style="color: var(--gold);">0</div>
        </div>
        <div class="stat-box">
          <div class="stat-label">Net Gold Delta</div>
          <div id="cvz-gold-delta" class="stat-val" style="color: var(--green);">+0</div>
        </div>
        <div class="stat-box">
          <div class="stat-label">Gems Balance</div>
          <div id="cvz-gems" class="stat-val" style="color: #38bdf8;">0</div>
          <div id="cvz-gems-delta" style="font-size: 11px; color: var(--green); margin-top: 4px; font-weight: 700;">+0 gems</div>
        </div>
      </div>
      <div class="stats-row">
        <div class="stat-box">
          <div class="stat-label">Current Wave</div>
          <div id="cvz-wave" class="stat-val" style="color: var(--gold);">Wave #1</div>
        </div>
        <div class="stat-box">
          <div class="stat-label">Waves Cleared</div>
          <div id="cvz-waves-cleared" class="stat-val" style="color: var(--green);">0</div>
          <div id="cvz-waves-lost" style="font-size: 11px; color: #94a3b8; margin-top: 4px;">0 lost</div>
        </div>
        <div class="stat-box">
          <div class="stat-label">Wall Repairs</div>
          <div id="cvz-wall-repairs" class="stat-val" style="color: var(--accent-cat);">0</div>
          <div id="cvz-squad-size" style="font-size: 11px; color: #38bdf8; margin-top: 4px;">Squad: 0 cats</div>
        </div>
      </div>

      <!-- Action & Tactical Status Banner -->
      <div style="background: rgba(255,255,255,0.03); border: 1px solid rgba(255,255,255,0.07); border-radius: 12px; padding: 12px 16px; display: flex; flex-direction: column; gap: 6px;">
        <div style="display: flex; justify-content: space-between; align-items: center;">
          <span style="font-size: 11px; color: var(--text-muted); text-transform: uppercase; font-weight: 700; letter-spacing: 0.5px;">Tactical Phase & Defense</span>
          <div style="display: flex; align-items: center; gap: 8px;">
            <span id="cvz-timer-rem" style="font-size: 11px; color: #94a3b8; font-family: 'JetBrains Mono', monospace;">Ready</span>
            <span id="cvz-action-badge" class="pill pill-cvz">DEFENSE READY</span>
          </div>
        </div>
        <div style="display: flex; justify-content: space-between; align-items: baseline; font-size: 13px;">
          <span id="cvz-action-status" style="font-weight: 600; color: #f1f5f9;">Engine operating normally</span>
        </div>
      </div>

      <div>
        <div style="display: flex; justify-content: space-between; font-size: 12px; margin-bottom: 4px; color: var(--text-muted);">
          <span>🏰 Fortress Wall Integrity</span>
          <span id="cvz-wall-hp-val">0 / 1000 HP (100%)</span>
        </div>
        <div class="meter-bar-container"><div id="cvz-wall-bar" class="meter-bar-fill fill-green" style="width: 100%;"></div></div>

        <div style="display: flex; justify-content: space-between; font-size: 12px; margin-top: 10px; margin-bottom: 4px; color: var(--text-muted);">
          <span>🌾 Daily Farming Cap (Gold)</span>
          <span id="cvz-daily-gold-val">0 / 0</span>
        </div>
        <div class="meter-bar-container"><div id="cvz-daily-gold-bar" class="meter-bar-fill fill-cyan" style="width: 0%;"></div></div>
      </div>

      <div>
        <div style="font-size: 12px; color: var(--text-muted); margin-bottom: 8px;">Live Engine Terminal Stream (Saved in logs/catvszombie_history.log)</div>
        <div id="cvz-logs" class="log-box"></div>
      </div>
    </div>

  </div>

  <div class="footer">
    <div id="uptime">Uptime: 00m 00s</div>
    <div>Target Chat: -1003959353966 (Topics: 2, 4 & 6) | Reports: Telegraph (Graph.org)</div>
    <div id="tg-status">Telegram Bot: Connecting...</div>
  </div>

  <script>
    function setText(id, val) {
      const el = document.getElementById(id);
      if (el) el.innerText = (val != null) ? val : '';
    }

    function setStyle(id, prop, val) {
      const el = document.getElementById(id);
      if (el && el.style) el.style[prop] = val;
    }

    function escapeHtml(text) {
      if (text == null) return '';
      return String(text).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    }

    async function updateDashboard() {
      try {
        const res = await fetch('/api/status');
        const data = await res.json();

        setText('countdown', `Next Hourly Report: ${data.countdown || '--:--'}`);
        setText('uptime', `Uptime: ${data.uptime || '00m 00s'}`);
        setText('tg-status', `Telegram Bot: ${data.tg_status || 'Offline'}`);

        // Catizen Updates
        const cat = data.catizen || {};
        setText('cat-state', cat.status || 'OFFLINE');
        const catBtn = document.getElementById('cat-btn');
        if (catBtn) {
          if (cat.enabled) {
            catBtn.className = 'btn-action btn-stop';
            catBtn.innerText = '🛑 Stop';
          } else {
            catBtn.className = 'btn-action btn-resume';
            catBtn.innerText = '▶️ Resume';
          }
        }

        const catOpenBtn = document.getElementById('cat-open-btn');
        if (catOpenBtn) {
          if (cat.web_app_url) {
            catOpenBtn.href = cat.web_app_url;
            catOpenBtn.style.display = 'inline-flex';
          } else {
            catOpenBtn.style.display = 'none';
          }
        }

        setText('cat-gold', (cat.gold || 0).toLocaleString());
        setText('cat-delta', ((cat.gold_delta >= 0 ? '+' : '') + (cat.gold_delta || 0).toLocaleString()));
        setText('cat-rate', `${(cat.gold_rate || 0).toLocaleString()}/s`);
        setText('cat-max', `Lvl ${cat.max_cat || 0}`);
        setText('cat-top', `Lvl ${cat.highest_on_board || 0}`);
        setText('cat-merges', cat.merges || 0);
        setText('cat-free-slots', `${cat.empty_slots || 0}/12 Slots Free`);

        // Render 12 Cat slots
        const grid = document.getElementById('cat-board-grid');
        if (grid) {
          grid.innerHTML = '';
          const board = (cat.board && cat.board.length === 12) ? cat.board : new Array(12).fill(0);
          board.forEach((lvl, idx) => {
            const div = document.createElement('div');
            div.className = 'cat-slot' + (lvl > 0 ? ' active' : '');
            div.innerText = lvl > 0 ? `Lvl ${lvl}` : `Slot ${idx}`;
            grid.appendChild(div);
          });
        }

        // Cat Logs
        const catLogs = document.getElementById('cat-logs');
        if (catLogs && Array.isArray(cat.logs)) {
          catLogs.innerHTML = cat.logs.map(l => `<div class="log-line"><span class="log-time">${l.time || ''}</span><span>${escapeHtml(l.text || '')}</span></div>`).join('');
          catLogs.scrollTop = catLogs.scrollHeight;
        }

        // Rignite Updates
        const rig = data.rignite || {};
        setText('rig-state', rig.status || 'OFFLINE');
        const rigBtn = document.getElementById('rig-btn');
        if (rigBtn) {
          if (rig.enabled) {
            rigBtn.className = 'btn-action btn-stop';
            rigBtn.innerText = '🛑 Stop';
          } else {
            rigBtn.className = 'btn-action btn-resume';
            rigBtn.innerText = '▶️ Resume';
          }
        }

        const rigOpenBtn = document.getElementById('rig-open-btn');
        if (rigOpenBtn) {
          if (rig.web_app_url) {
            rigOpenBtn.href = rig.web_app_url;
            rigOpenBtn.style.display = 'inline-flex';
          } else {
            rigOpenBtn.style.display = 'none';
          }
        }
        setText('rig-coins', (rig.coins || 0).toLocaleString());
        setText('rig-delta', ((rig.coins_delta >= 0 ? '+' : '') + (rig.coins_delta || 0).toLocaleString()));
        setText('rig-rate', `${(rig.coins_rate || 0).toFixed(1)}/s`);
        setText('rig-rate-cpm', `${(rig.coins_cpm || 0).toLocaleString()} coins/min`);
        setText('rig-taps', (rig.session_taps || 0).toLocaleString());
        setText('rig-tap-cadence', `${(rig.tap_cadence || 0).toFixed(1)} taps/s cadence`);
        setText('rig-colony-coins', (rig.colony_coins_total || 0).toLocaleString());
        setText('rig-boosts', `${rig.boosts || 0} Instant`);
        setText('rig-ad-boosts', `Ad Refills: ${(rig.ad_energy_left || 0).toLocaleString()} left`);
        setText('rig-cycle', `#${rig.cycle || 1}`);
        setText('rig-batches', `${rig.batches_synced || 0} batches synced`);

        setText('rig-energy-val', `${rig.energy || 0} / ${rig.max_energy || 1000}`);
        const ePct = Math.min(100, Math.max(0, ((rig.energy || 0) / Math.max(1, rig.max_energy || 1000)) * 100));
        setStyle('rig-energy-bar', 'width', `${ePct}%`);

        setText('rig-battery-val', `${(rig.battery_energy || 0).toLocaleString()} / ${(rig.battery_cap || 0).toLocaleString()}`);
        const bPct = (rig.battery_cap > 0) ? Math.min(100, Math.max(0, ((rig.battery_energy || 0) / rig.battery_cap) * 100)) : 0;
        setStyle('rig-battery-bar', 'width', `${bPct}%`);

        // Refill and Action details
        const actionStatus = rig.action_status || 'Engine active';
        const nextAction = rig.next_action || 'Ready';
        setText('rig-action-status', actionStatus);
        setText('rig-next-action', nextAction);
        
        const actionBadge = document.getElementById('rig-action-badge');
        if (actionBadge) {
          if (actionStatus.toLowerCase().includes('ad')) {
            actionBadge.innerText = 'WATCHING AD REFILL';
            actionBadge.style.color = '#38bdf8';
            actionBadge.style.borderColor = 'rgba(56, 189, 248, 0.4)';
            actionBadge.style.background = 'rgba(56, 189, 248, 0.2)';
          } else if (actionStatus.toLowerCase().includes('recharg') || actionStatus.toLowerCase().includes('rest')) {
            actionBadge.innerText = 'RECHARGING REST';
            actionBadge.style.color = '#fbbf24';
            actionBadge.style.borderColor = 'rgba(251, 191, 36, 0.4)';
            actionBadge.style.background = 'rgba(251, 191, 36, 0.2)';
          } else if (actionStatus.toLowerCase().includes('daily') || actionStatus.toLowerCase().includes('refill')) {
            actionBadge.innerText = 'INSTANT REFILL';
            actionBadge.style.color = '#4ade80';
            actionBadge.style.borderColor = 'rgba(74, 222, 128, 0.4)';
            actionBadge.style.background = 'rgba(74, 222, 128, 0.2)';
          } else {
            actionBadge.innerText = 'BURST TAPPING';
            actionBadge.style.color = '#c084fc';
            actionBadge.style.borderColor = 'rgba(192, 132, 252, 0.4)';
            actionBadge.style.background = 'rgba(192, 132, 252, 0.2)';
          }
        }

        // Rig Logs
        const rigLogs = document.getElementById('rig-logs');
        if (rigLogs && Array.isArray(rig.logs)) {
          rigLogs.innerHTML = rig.logs.map(l => `<div class="log-line"><span class="log-time">${l.time || ''}</span><span>${escapeHtml(l.text || '')}</span></div>`).join('');
          rigLogs.scrollTop = rigLogs.scrollHeight;
        }

        // Cats vs Zombies Updates
        const cvz = data.catvszombie || {};
        setText('cvz-state', cvz.status || 'OFFLINE');
        const cvzBtn = document.getElementById('cvz-btn');
        if (cvzBtn) {
          if (cvz.enabled) {
            cvzBtn.className = 'btn-action btn-stop';
            cvzBtn.innerText = '🛑 Stop';
          } else {
            cvzBtn.className = 'btn-action btn-resume';
            cvzBtn.innerText = '▶️ Resume';
          }
        }

        const cvzOpenBtn = document.getElementById('cvz-open-btn');
        if (cvzOpenBtn) {
          if (cvz.web_app_url) {
            cvzOpenBtn.href = cvz.web_app_url;
            cvzOpenBtn.style.display = 'inline-flex';
          } else {
            cvzOpenBtn.style.display = 'none';
          }
        }

        setText('cvz-gold', (cvz.gold || 0).toLocaleString());
        setText('cvz-gold-delta', ((cvz.gold_delta >= 0 ? '+' : '') + (cvz.gold_delta || 0).toLocaleString()));
        setText('cvz-gems', (cvz.gems || 0).toLocaleString());
        setText('cvz-gems-delta', ((cvz.gems_delta >= 0 ? '+' : '') + (cvz.gems_delta || 0).toLocaleString()));
        setText('cvz-wave', `Wave #${cvz.wave || 1}`);
        setText('cvz-waves-cleared', (cvz.waves_cleared || 0).toLocaleString());
        setText('cvz-wall-repairs', (cvz.wall_repairs || 0).toLocaleString());
        setText('cvz-waves-lost', `${cvz.waves_lost || 0} lost`);
        setText('cvz-squad-size', `Squad: ${cvz.cats_equipped || 0} cats`);
        setText('cvz-timer-rem', cvz.repair_rem > 0 ? `Repair: ${cvz.repair_rem}s` : (cvz.cooldown_rem > 0 ? `Cooldown: ${cvz.cooldown_rem}s` : 'Ready'));

        // Wall HP Bar
        const wallHp = cvz.wall_hp || 0;
        const wallMax = Math.max(1, cvz.wall_max_hp || 1000);
        const wallPct = Math.min(100, Math.max(0, Math.round((wallHp / wallMax) * 100)));
        setText('cvz-wall-hp-val', `${wallHp.toLocaleString()} / ${wallMax.toLocaleString()} HP (${wallPct}%)`);
        setStyle('cvz-wall-bar', 'width', `${wallPct}%`);
        const wallBar = document.getElementById('cvz-wall-bar');
        if (wallBar) {
          if (wallPct > 50) {
            wallBar.style.background = 'linear-gradient(90deg, #10b981, #34d399)';
          } else if (wallPct > 20) {
            wallBar.style.background = 'linear-gradient(90deg, #f59e0b, #fbbf24)';
          } else {
            wallBar.style.background = 'linear-gradient(90deg, #ef4444, #f87171)';
          }
        }

        // Action Status
        const cvzAction = cvz.action_status || 'Engine active';
        setText('cvz-action-status', cvzAction);
        const cvzBadge = document.getElementById('cvz-action-badge');
        if (cvzBadge) {
          if (cvzAction.toLowerCase().includes('repair')) {
            cvzBadge.innerText = 'REPAIRING';
            cvzBadge.style.color = '#fbbf24';
            cvzBadge.style.borderColor = 'rgba(251, 191, 36, 0.4)';
            cvzBadge.style.background = 'rgba(251, 191, 36, 0.2)';
          } else if (cvzAction.toLowerCase().includes('cooldown')) {
            cvzBadge.innerText = 'COOLDOWN';
            cvzBadge.style.color = '#38bdf8';
            cvzBadge.style.borderColor = 'rgba(56, 189, 248, 0.4)';
            cvzBadge.style.background = 'rgba(56, 189, 248, 0.2)';
          } else if (cvzAction.toLowerCase().includes('battle') || cvzAction.toLowerCase().includes('engag')) {
            cvzBadge.innerText = 'IN COMBAT';
            cvzBadge.style.color = '#ef4444';
            cvzBadge.style.borderColor = 'rgba(239, 68, 68, 0.4)';
            cvzBadge.style.background = 'rgba(239, 68, 68, 0.2)';
          } else {
            cvzBadge.innerText = 'DEFENSE READY';
            cvzBadge.style.color = '#10b981';
            cvzBadge.style.borderColor = 'rgba(16, 185, 129, 0.4)';
            cvzBadge.style.background = 'rgba(16, 185, 129, 0.2)';
          }
        }

        // Daily Caps
        setText('cvz-daily-gold-val', `${(cvz.daily_gold || 0).toLocaleString()} / ${(cvz.daily_gold_cap || 0).toLocaleString()}`);
        const dgPct = (cvz.daily_gold_cap > 0) ? Math.min(100, Math.round(((cvz.daily_gold || 0) / cvz.daily_gold_cap) * 100)) : 0;
        setStyle('cvz-daily-gold-bar', 'width', `${dgPct}%`);

        setText('cvz-daily-gems-val', `${(cvz.daily_gems || 0).toLocaleString()} / ${(cvz.daily_gems_cap || 0).toLocaleString()}`);
        const dmPct = (cvz.daily_gems_cap > 0) ? Math.min(100, Math.round(((cvz.daily_gems || 0) / cvz.daily_gems_cap) * 100)) : 0;
        setStyle('cvz-daily-gems-bar', 'width', `${dmPct}%`);

        // CvZ Logs
        const cvzLogs = document.getElementById('cvz-logs');
        if (cvzLogs && Array.isArray(cvz.logs)) {
          cvzLogs.innerHTML = cvz.logs.map(l => `<div class="log-line"><span class="log-time">${l.time || ''}</span><span>${escapeHtml(l.text || '')}</span></div>`).join('');
          cvzLogs.scrollTop = cvzLogs.scrollHeight;
        }


      } catch (err) {
        console.error('Update failed:', err);
      }
    }

    async function toggleGame(game) {
      try {
        let btn = null;
        if (game === 'catizen') btn = document.getElementById('cat-btn');
        else if (game === 'rignite') btn = document.getElementById('rig-btn');
        else if (game === 'catvszombie') btn = document.getElementById('cvz-btn');
        const isStop = btn.innerText.includes('Stop');
        const action = isStop ? 'stop' : 'resume';
        btn.disabled = true;
        await fetch(`/api/control?game=${game}&action=${action}`, { method: 'POST' });
        setTimeout(() => { btn.disabled = false; updateDashboard(); }, 500);
      } catch (e) {
        console.error('Toggle error:', e);
      }
    }


    async function triggerCvzAction(action) {
      const btnId = action === 'repair_wall' ? 'cvz-repair-btn' : 'cvz-claim-btn';
      const btn = document.getElementById(btnId);
      try {
        if (btn) btn.disabled = true;
        await fetch(`/api/control?game=catvszombie&action=${action}`, { method: 'POST' });
        setTimeout(() => { if (btn) btn.disabled = false; updateDashboard(); }, 1000);
      } catch (e) {
        console.error('Action error:', e);
        if (btn) btn.disabled = false;
      }
    }

    async function triggerManualAdRefill() {
      const btn = document.getElementById('rig-ad-btn');
      try {
        btn.disabled = true;
        btn.innerText = '⏳ Triggering...';
        const res = await fetch('/api/control?game=rignite&action=watch_ad_refill', { method: 'POST' });
        const data = await res.json();
        if (data.ok) {
          btn.innerText = '🎬 Ad Sent!';
          setTimeout(() => { btn.innerText = '🎬 Watch Ad Refill'; btn.disabled = false; }, 3000);
        } else {
          btn.innerText = '⚠️ Failed';
          setTimeout(() => { btn.innerText = '🎬 Watch Ad Refill'; btn.disabled = false; }, 2000);
        }
        updateDashboard();
      } catch (e) {
        console.error('Trigger ad refill error:', e);
        btn.innerText = '🎬 Watch Ad Refill';
        btn.disabled = false;
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
        self.catvszombie_tracker = GameTracker("CatVSZombie", CATVSZOMBIE_LOG_FILE)
        self.telegraph = TelegraphManager()
        self.loop = asyncio.new_event_loop()
        self.seconds_until_next_report = REPORT_INTERVAL_SECONDS
        self.last_tg_status: str = "Connecting..."
        self.is_bot_online: bool = False
        self.flood_wait_until: float = 0.0
        
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

                    cleaned = line.strip()
                    if not cleaned:
                        continue

                    # Record line in local history file and rolling buffer
                    tracker.add_log(cleaned)
                    # Also print clean summary to console
                    sys.stdout.write(f"[{label}] {cleaned}\n")
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

        # CatVSZombie stats
        z_state = self.catvszombie_tracker.current_state
        z_init = self.catvszombie_tracker.start_state
        z_curr_gold = z_state.get("gold", 0)
        z_start_gold = z_init.get("gold", z_curr_gold)
        z_gold_delta = z_state.get("gold_delta", z_curr_gold - z_start_gold)
        z_curr_gems = z_state.get("gems", 0)
        z_start_gems = z_init.get("gems", z_curr_gems)
        z_gems_delta = z_state.get("gems_delta", z_curr_gems - z_start_gems)

        # Rignite stats
        r_state = self.rignite_tracker.current_state
        r_init = self.rignite_tracker.start_state
        r_curr_coins = r_state.get("coins", 0)
        r_start_coins = r_init.get("coins", r_curr_coins)
        
        # Prefer engine-calculated delta & rate if present, else fallback
        r_coins_delta = r_state.get("coins_delta", r_curr_coins - r_start_coins)
        r_coins_rate = r_state.get("rate_cps", max(0, r_coins_delta) / session_sec)
        r_coins_cpm = r_state.get("rate_cpm", int(r_coins_rate * 60.0))
        r_taps = r_state.get("session_taps", 0)
        r_cadence = r_state.get("tap_cadence", (r_taps / session_sec) if session_sec > 0 else 0.0)
        r_batches = r_state.get("batches_sent", sum(1 for e in self.rignite_tracker.events_in_period if e["data"].get("type") == "tap_sync"))

        return {
            "uptime": uptime_fmt,
            "countdown": countdown_str,
            "tg_status": self.last_tg_status,
            "catizen": {
                "status": self.catizen_tracker.status_pill,
                "enabled": self.catizen_tracker.is_enabled,
                "web_app_url": c_state.get("web_app_url", ""),
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
                "web_app_url": r_state.get("web_app_url", ""),
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
                "ad_boost_left": r_state.get("ad_boost_left", 0),
                "tap_mult": r_state.get("tap_mult", 1),
                "action_status": r_state.get("action_status", "Tapping Active"),
                "next_action": r_state.get("next_action", "Ready"),
                "batches_synced": r_batches,
                "colony_coins_total": r_state.get("colony_coins_total", 0),
                "cycle": r_state.get("cycle_count", 1),
                "logs": self.rignite_tracker.raw_logs[-35:]
            },
            "catvszombie": {
                "status": self.catvszombie_tracker.status_pill,
                "enabled": self.catvszombie_tracker.is_enabled,
                "web_app_url": z_state.get("web_app_url", ""),
                "wave": z_state.get("wave", 1),
                "gold": z_curr_gold,
                "gold_delta": z_gold_delta,
                "gems": z_curr_gems,
                "gems_delta": z_gems_delta,
                "wall_hp": z_state.get("wall_hp", 0),
                "wall_max_hp": z_state.get("wall_max_hp", 1000),
                "wall_pct": z_state.get("wall_pct", 100),
                "cooldown_rem": z_state.get("cooldown_rem", 0),
                "repair_rem": z_state.get("repair_rem", 0),
                "waves_cleared": z_state.get("waves_cleared", 0),
                "waves_lost": z_state.get("waves_lost", 0),
                "wall_repairs": z_state.get("wall_repairs", 0),
                "daily_gold": z_state.get("daily_gold", 0),
                "daily_gold_cap": z_state.get("daily_gold_cap", 0),
                "daily_gems": z_state.get("daily_gems", 0),
                "daily_gems_cap": z_state.get("daily_gems_cap", 0),
                "cats_equipped": z_state.get("cats_equipped", 0),
                "action_status": z_state.get("action_status", "Active"),
                "logs": self.catvszombie_tracker.raw_logs[-35:]
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
                elif game == "catvszombie":
                    target = self.catvszombie_tracker

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
                    target.reset_metrics_baseline()
                    target.add_log(f"▶️ [CONTROL] Autoplay resumed via dashboard. Metrics reset for fresh session.")
                    return web.json_response({"ok": True, "status": "resumed", "game": game})
                elif action == "watch_ad_refill" and game == "rignite":
                    # Write command to IPC file for rignite engine to consume
                    cmd_file = os.path.join(LOGS_DIR, "rignite_cmd.json")
                    try:
                        with open(cmd_file, "w", encoding="utf-8") as f:
                            json.dump({"action": "watch_ad_refill", "ts": time.time()}, f)
                        target.add_log("🎬 [CONTROL] Manual watch ad & refill requested from Dashboard.")
                        return web.json_response({"ok": True, "status": "ad_command_queued", "game": game})
                    except Exception as ce:
                        return web.json_response({"ok": False, "error": str(ce)}, status=500)
                elif action in ["repair_wall", "claim_tasks"] and game == "catvszombie":
                    cmd_file = os.path.join(LOGS_DIR, "catvszombie_cmd.json")
                    try:
                        with open(cmd_file, "w", encoding="utf-8") as f:
                            json.dump({"action": action, "ts": time.time()}, f)
                        target.add_log(f"🕹️ [CONTROL] Command '{action}' queued from Dashboard.")
                        return web.json_response({"ok": True, "status": f"{action}_queued", "game": game})
                    except Exception as ce:
                        return web.json_response({"ok": False, "error": str(ce)}, status=500)
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
    async def safe_send_topic_message(self, topic_id: int, text: str):
        """Send message to forum topic with automatic compatibility fallback."""
        try:
            return await self.client.send_message(
                chat_id=TARGET_CHAT_ID,
                message_thread_id=topic_id,
                text=text,
                parse_mode=enums.ParseMode.HTML,
                disable_web_page_preview=True
            )
        except TypeError as e:
            if "message_thread_id" in str(e):
                # Older Pyrogram versions use reply_to_message_id for forum topic threads
                return await self.client.send_message(
                    chat_id=TARGET_CHAT_ID,
                    reply_to_message_id=topic_id,
                    text=text,
                    parse_mode=enums.ParseMode.HTML,
                    disable_web_page_preview=True
                )
            raise e

    async def send_hourly_reports(self):
        now_ist = datetime.now(IST_TZ)
        cat_time_from = self.catizen_tracker.period_start_time.strftime("%I:%M %p")
        cat_time_to = now_ist.strftime("%I:%M %p IST")
        date_str = now_ist.strftime("%d %b %Y")

        # ---------------- CATIZEN REPORT ----------------
        try:
            cat_url = self.generate_catizen_telegraph(date_str, cat_time_from, cat_time_to)
            cat_text = self.build_catizen_tg_message(date_str, cat_time_from, cat_time_to, cat_url)
            await self.safe_send_topic_message(TOPIC_CATIZEN, cat_text)
            self.catizen_tracker.reset_period()
        except Exception as e:
            self.catizen_tracker.add_log(f"Telegram dispatch error: {e}")

        # ---------------- RIGNITE REPORT ----------------
        try:
            rig_url = self.generate_rignite_telegraph(date_str, cat_time_from, cat_time_to)
            rig_text = self.build_rignite_tg_message(date_str, cat_time_from, cat_time_to, rig_url)
            await self.safe_send_topic_message(TOPIC_RIGNITE, rig_text)
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
    # CAT VS ZOMBIE MESSAGES & TELEGRAPH BUILDERS
    # --------------------------------------------------------------------------
    def build_catvszombie_tg_message(self, date_str: str, time_from: str, time_to: str, telegraph_url: Optional[str]) -> str:
        curr = self.catvszombie_tracker.current_state
        prev = self.catvszombie_tracker.period_start_state

        curr_gold = curr.get("gold", 0)
        prev_gold = prev.get("gold", curr_gold)
        gold_diff = curr_gold - prev_gold

        curr_gems = curr.get("gems", 0)
        prev_gems = prev.get("gems", curr_gems)
        gems_diff = curr_gems - prev_gems

        wave = curr.get("wave", 1)
        wall_hp = curr.get("wall_hp", 0)
        wall_max_hp = curr.get("wall_max_hp", 1000)
        waves_cleared = curr.get("waves_cleared", 0)
        wall_repairs = curr.get("wall_repairs", 0)
        daily_gold = curr.get("daily_gold", 0)
        daily_gold_cap = curr.get("daily_gold_cap", 0)
        daily_gems = curr.get("daily_gems", 0)
        daily_gems_cap = curr.get("daily_gems_cap", 0)
        restarts = self.catvszombie_tracker.restart_count

        delta_sign_g = "+" if gold_diff >= 0 else ""
        delta_sign_m = "+" if gems_diff >= 0 else ""
        link_line = f"\n🌐 <b>Full Hourly Audit:</b> <a href=\"{telegraph_url}\">View Detailed Telegraph Report</a>\n" if telegraph_url else ""

        return (
            f"🧟 <b>CATS VS ZOMBIES BATTLE OVERVIEW</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📅 <b>Date:</b> {date_str}\n"
            f"⏰ <b>Window:</b> <code>{time_from}</code> ➔ <code>{time_to}</code>\n"
            f"🟢 <b>Status:</b> <code>WAVE COMBAT & DEFENSE</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🛡️ <b>Current Wave:</b> <b>Wave #{wave}</b>\n"
            f"🏰 <b>Fortress Wall:</b> <code>{wall_hp:,} / {wall_max_hp:,} HP</code>\n"
            f"💰 <b>Gold Balance:</b> <code>{curr_gold:,}</code> (<code>{delta_sign_g}{gold_diff:,}</code>)\n"
            f"💎 <b>Gems Balance:</b> <code>{curr_gems:,}</code> (<code>{delta_sign_m}{gems_diff:,}</code>)\n"
            f"🌾 <b>Daily Farm:</b> Gold: <code>{daily_gold}/{daily_gold_cap}</code> | Gems: <code>{daily_gems}/{daily_gems_cap}</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🚀 <b>1-Hour Performance:</b>\n"
            f"  • Waves Cleared: <b>{waves_cleared}</b>\n"
            f"  • Fortress Wall Repairs: <b>{wall_repairs}</b>\n"
            f"  • Engine Restarts: <b>{restarts}</b>\n"
            f"{link_line}"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"<i>Auto-Player Sentinel • Asia/Kolkata (IST)</i>"
        )

    def generate_catvszombie_telegraph(self, date_str: str, time_from: str, time_to: str) -> Optional[str]:
        curr = self.catvszombie_tracker.current_state
        prev = self.catvszombie_tracker.period_start_state
        curr_gold = curr.get("gold", 0)
        prev_gold = prev.get("gold", curr_gold)
        gold_diff = curr_gold - prev_gold

        recent_events = self.catvszombie_tracker.events_in_period[-40:]
        event_nodes = []
        for ev in reversed(recent_events):
            t = ev.get("time", "")
            d = ev.get("data", {})
            ev_type = d.get("type", "event")
            event_nodes.append({
                "tag": "li",
                "children": [f"[{t}] Type: {ev_type} | Wave: #{d.get('wave', 1)} | Gold: {d.get('gold', 0):,} | Wall: {d.get('wall_hp', 0)}/{d.get('wall_max_hp', 1000)}"]
            })

        if not event_nodes:
            event_nodes.append({"tag": "li", "children": ["No combat events recorded in this hourly slice."]})

        nodes = [
            {"tag": "h3", "children": [f"Cats vs Zombies Detailed Status ({date_str})"]},
            {"tag": "p", "children": [f"Interval: {time_from} to {time_to} (IST)"]},
            {"tag": "h4", "children": ["Tactical Defense & Economy Snapshot"]},
            {
                "tag": "ul",
                "children": [
                    {"tag": "li", "children": [f"Current Wave: Wave #{curr.get('wave', 1)}"]},
                    {"tag": "li", "children": [f"Fortress Wall: {curr.get('wall_hp', 0):,} / {curr.get('wall_max_hp', 1000):,} HP"]},
                    {"tag": "li", "children": [f"Gold Balance: {curr_gold:,} (Net: {'+' if gold_diff >= 0 else ''}{gold_diff:,})"]},
                    {"tag": "li", "children": [f"Gems Balance: {curr.get('gems', 0):,}"]},
                    {"tag": "li", "children": [f"Waves Cleared (Session): {curr.get('waves_cleared', 0):,}"]},
                    {"tag": "li", "children": [f"Wall Repairs: {curr.get('wall_repairs', 0)}"]},
                    {"tag": "li", "children": [f"Supervisor Restarts: {self.catvszombie_tracker.restart_count}"]}
                ]
            },
            {"tag": "h4", "children": ["Chronological Battle Activity Log (Last 40 Events)"]},
            {"tag": "ul", "children": event_nodes}
        ]

        return self.telegraph.create_page(
            title=f"Cats vs Zombies Hourly Report - {date_str} {time_to}",
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
            await self.safe_send_topic_message(TOPIC_CATIZEN, catizen_msg)
        except Exception as e:
            self.catizen_tracker.add_log(f"Catizen startup alert error: {e}")

        try:
            await self.safe_send_topic_message(TOPIC_RIGNITE, rignite_msg)
        except Exception as e:
            self.rignite_tracker.add_log(f"Rignite startup alert error: {e}")

        cvz_msg = (
            f"🧟 <b>CATS VS ZOMBIES SENTINEL ONLINE</b> 🟢\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🚀 <b>Auto-Player Supervisor:</b> Initialized\n"
            f"🕒 <b>Start Time:</b> <code>{now_ist}</code>\n"
            f"⚙️ <b>Engine Target:</b> <code>CatVSZombie/cat_vs_zombie_engine.py</code>\n"
            f"⏱️ <b>Hourly Audit Window:</b> Every 60 Minutes\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"<i>Infinite wave combat, automated wall repair, & task claimer active.</i>"
        )
        try:
            await self.safe_send_topic_message(TOPIC_CATVSZOMBIE, cvz_msg)
        except Exception as e:
            self.catvszombie_tracker.add_log(f"CatVSZombie startup alert error: {e}")

    # --------------------------------------------------------------------------
    # BACKGROUND SCHEDULE RUNNER & WEB SERVER
    # --------------------------------------------------------------------------
    async def scheduler_loop(self):
        # 1. Start Web Dashboard immediately regardless of Telegram status
        web_runner = await self.init_web_app()

        # 2. Start Self-Pinger Background Task
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

        # 3. Resilient Telegram Bot Connect / Reconnect Loop (Handles FloodWait & auto-retry)
        async def telegram_bot_worker():
            startup_sent = False
            while self.is_running:
                if not self.is_bot_online:
                    try:
                        self.last_tg_status = "Connecting to Telegram..."
                        await self.client.start()
                        self.is_bot_online = True
                        self.flood_wait_until = 0.0
                        self.last_tg_status = "Connected & Active"
                        print("🤖 [Telegram Bot] Connected successfully.")

                        if not startup_sent:
                            await self.send_startup_messages()
                            startup_sent = True
                    except FloodWait as fw:
                        wait_sec = int(fw.value)
                        self.flood_wait_until = time.time() + wait_sec
                        self.is_bot_online = False
                        msg = f"FloodWait ({wait_sec}s required). Gameplay continuing uninhibited..."
                        self.last_tg_status = f"Rate-Limited ({wait_sec}s remaining)"
                        print(f"⚠️ [Telegram Bot] {msg}")
                        FileLogger.append_log(os.path.join(LOGS_DIR, "telegram_bot.log"), msg)
                        await asyncio.sleep(wait_sec + 2)
                        continue
                    except Exception as ex:
                        self.is_bot_online = False
                        self.last_tg_status = f"Connect Error: {ex}"
                        print(f"⚠️ [Telegram Bot] Error: {ex}. Retrying in 30s...")
                        await asyncio.sleep(30)
                        continue

                await asyncio.sleep(5)

        asyncio.create_task(telegram_bot_worker())

        # 4. Hourly Reporting Countdown & Trigger Loop
        while self.is_running:
            try:
                for _ in range(REPORT_INTERVAL_SECONDS):
                    if not self.is_running:
                        break
                    self.seconds_until_next_report -= 1

                    # Update live FloodWait countdown display in status if waiting
                    if not self.is_bot_online and self.flood_wait_until > time.time():
                        rem = int(self.flood_wait_until - time.time())
                        self.last_tg_status = f"FloodWait ({rem // 60}m {rem % 60}s remaining - Gameplay Active)"

                    await asyncio.sleep(1)

                if self.is_running:
                    self.seconds_until_next_report = REPORT_INTERVAL_SECONDS
                    if self.is_bot_online:
                        await self.send_hourly_reports()
                    else:
                        print(f"⏳ [Hourly Report] Telegram Bot is currently offline/waiting. Game data preserved in /logs/.")
            except Exception as e:
                await asyncio.sleep(5)

        await web_runner.cleanup()
        if self.is_bot_online:
            try:
                await self.client.stop()
            except Exception:
                pass

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

            CVZ_TIMEOUT_SEC = 180
            if self.catvszombie_tracker.proc_handle and (now - self.catvszombie_tracker.last_heartbeat > CVZ_TIMEOUT_SEC):
                self.catvszombie_tracker.add_log(f"⚠️ [WATCHDOG] CatVSZombie process silent > {CVZ_TIMEOUT_SEC}s. Forcing restart...")
                try:
                    self.catvszombie_tracker.proc_handle.terminate()
                except Exception:
                    pass
                self.catvszombie_tracker.last_heartbeat = now

    def run(self):
        print("=" * 65)
        print("🚀 Auto-Player Central Supervisor Starting")
        print(f"🕒 Timezone: Asia/Kolkata (IST)")
        print(f"📢 Telegram Chat: {TARGET_CHAT_ID} (Topics: 2, 4 & 6)")
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

        # 3. Start Cat vs Zombie Engine Thread
        cvz_cmd = [sys.executable, CATVSZOMBIE_SCRIPT]
        cvz_thread = threading.Thread(
            target=self.start_process,
            args=(cvz_cmd, os.path.dirname(CATVSZOMBIE_SCRIPT), self.catvszombie_tracker, "CatVSZombie"),
            daemon=True
        )
        cvz_thread.start()

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
