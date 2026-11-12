"""
Cats vs Zombies Autonomous Battle & Progression Engine (Infinite Auto-Pilot)
----------------------------------------------------------------------------
Reverse-engineered from Cats vs Zombies Mini-App client (catsvszombies.uk):
- Target Bot: @CatsVSZombies_bot (t.me/CatsVSZombies_bot?startapp)
- API Base: https://api.catsvszombies.uk
- Session Maker: https://tg-game-sessionmaker.onrender.com

Autonomous Infinite Loop Architecture:
1. Auto-Handshake: Automatically fetches fresh Telegram initData via SessionMaker and logs in.
2. Mandatory Headers: Uses 'X-Cvz-Client: 2' and valid browser origins to bypass maintenance gates.
3. Wall Defense State Machine:
   - Detects wall breaches (wallHp <= 0) and issues POST /wave/repair-wall.
   - Monitors repair countdown (repairRemainingMs) with smooth second-by-second countdown.
4. Cooldown Watchdog:
   - Tracks wave post-combat cooldown (cooldownRemainingMs) before triggering the next wave.
5. Combat Simulation:
   - Dispatches POST /wave/play when wall is repaired and cooldown is expired.
   - Simulates human-like combat engagement delay (2-4 seconds) to match server battle events.
6. Daily Tasks & Milestones Auto-Claimer:
   - Claims daily checkin, goodmorning, displayname rewards.
   - Claims unlocked milestone achievement rewards periodically.
7. Observability & IPC:
   - Emits structured [AUTOPLAYER_METRIC] telemetry JSON for auto_player.py dashboard.
   - Listens to logs/catvszombie_cmd.json for instant remote commands (repair, claim, pause).
"""

import sys
import os
import time
import math
import random
import json
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List

# Ensure stdout and stderr handle UTF-8 cleanly on Windows
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

console = Console(legacy_windows=False)

# Configuration & Endpoints
API_BASE_URL = "https://api.catsvszombies.uk"
LOGIN_ENDPOINT = f"{API_BASE_URL}/login"
HOME_STATUS_ENDPOINT = f"{API_BASE_URL}/home/status"
WAVE_PLAY_ENDPOINT = f"{API_BASE_URL}/wave/play"
WAVE_REPAIR_ENDPOINT = f"{API_BASE_URL}/wave/repair-wall"
TASKS_STATUS_ENDPOINT = f"{API_BASE_URL}/tasks/status"
TASKS_CLAIM_DAILY = f"{API_BASE_URL}/tasks/claim/daily"
TASKS_CLAIM_CHEST = f"{API_BASE_URL}/tasks/claim/chest"
TASKS_CLAIM_MILESTONE = f"{API_BASE_URL}/tasks/claim/milestone"

SESSION_MAKER_URL = "https://tg-game-sessionmaker.onrender.com/api/get-init-data"
DEFAULT_BOT = "CatsVSZombies_bot"
CLIENT_MARK = "2"

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGS_DIR = os.path.join(ROOT_DIR, "logs")
os.makedirs(LOGS_DIR, exist_ok=True)
CMD_FILE = os.path.join(LOGS_DIR, "catvszombie_cmd.json")

IST_TZ = timezone(timedelta(hours=5, minutes=30))


def create_robust_session() -> requests.Session:
    """Creates a requests session configured with retries and DNS tolerance."""
    session = requests.Session()
    retry_strategy = Retry(
        total=4,
        backoff_factor=1.5,
        status_forcelist=[500, 502, 504],
        raise_on_status=False
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


class CatVSZombieEngine:
    def __init__(self, bot_username: str = DEFAULT_BOT):
        self.bot_username = bot_username
        self.session = create_robust_session()
        self.init_data: Optional[str] = None
        self.web_app_url: Optional[str] = None
        
        # User State
        self.user_id: Optional[str] = None
        self.username: str = "Unknown"
        self.gold: int = 0
        self.gems: int = 0
        self.start_gold: int = 0
        self.start_gems: int = 0
        
        # Combat State
        self.current_wave: int = 1
        self.wall_hp: int = 1000
        self.wall_max_hp: int = 1000
        self.equipped_cats: List[Dict[str, Any]] = []
        self.cooldown_ends_at: float = 0.0
        self.repair_ends_at: float = 0.0
        self.action_status: str = "Initializing..."
        
        # Daily & Session Counters
        self.daily_gold_farmed: int = 0
        self.daily_gold_cap: int = 0
        self.daily_gems_farmed: int = 0
        self.daily_gems_cap: int = 0
        self.waves_cleared: int = 0
        self.waves_lost: int = 0
        self.wall_repairs: int = 0
        self.last_task_check: float = 0.0
        self.last_status_sync: float = 0.0
        self.session_start: float = time.time()
        self.is_paused: bool = False

    @property
    def default_headers(self) -> Dict[str, str]:
        return {
            "Content-Type": "application/json",
            "X-Cvz-Client": CLIENT_MARK,
            "Origin": "https://catsvszombies.uk",
            "Referer": "https://catsvszombies.uk/",
            "User-Agent": "Mozilla/5.0 (Linux; Android 14; Mobile; rv:130.0) Gecko/130.0 Firefox/130.0"
        }

    def log(self, text: str, style: str = "white"):
        now_str = datetime.now(IST_TZ).strftime("%H:%M:%S")
        console.print(f"[{style}][{now_str}] {text}[/{style}]")

    def emit_telemetry(self, event_type: str = "sync", extra: Optional[Dict[str, Any]] = None):
        """Emits structured [AUTOPLAYER_METRIC] JSON for central dashboard ingestion."""
        wall_pct = int((self.wall_hp / max(1, self.wall_max_hp)) * 100)
        cd_rem = max(0.0, self.cooldown_ends_at - time.time())
        rep_rem = max(0.0, self.repair_ends_at - time.time())

        payload = {
            "type": event_type,
            "wave": self.current_wave,
            "gold": self.gold,
            "gems": self.gems,
            "gold_delta": max(0, self.gold - self.start_gold),
            "gems_delta": max(0, self.gems - self.start_gems),
            "wall_hp": self.wall_hp,
            "wall_max_hp": self.wall_max_hp,
            "wall_pct": wall_pct,
            "cooldown_rem": int(cd_rem),
            "repair_rem": int(rep_rem),
            "waves_cleared": self.waves_cleared,
            "waves_lost": self.waves_lost,
            "wall_repairs": self.wall_repairs,
            "daily_gold": self.daily_gold_farmed,
            "daily_gold_cap": self.daily_gold_cap,
            "daily_gems": self.daily_gems_farmed,
            "daily_gems_cap": self.daily_gems_cap,
            "cats_equipped": len(self.equipped_cats),
            "action_status": self.action_status,
            "web_app_url": self.web_app_url or ""
        }
        if extra:
            payload.update(extra)

        sys.stdout.write(f"[AUTOPLAYER_METRIC] {json.dumps(payload)}\n")
        sys.stdout.flush()

    def fetch_fresh_init_data(self) -> bool:
        """Retrieves verified cryptographic Telegram initData via SessionMaker API."""
        self.log(f"🔑 Requesting fresh Telegram session from SessionMaker for @{self.bot_username}...", style="cyan")
        payload = {"bot": self.bot_username}
        try:
            resp = self.session.post(SESSION_MAKER_URL, json=payload, timeout=30)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("success") and data.get("initData"):
                    self.init_data = data["initData"]
                    self.web_app_url = data.get("launch_url") or data.get("url")
                    self.log(f"✅ Handshake token secured successfully (Length: {len(self.init_data)}).", style="green")
                    return True
                else:
                    self.log(f"❌ SessionMaker error: {data.get('error', 'Unknown')}", style="bold red")
            else:
                self.log(f"❌ SessionMaker HTTP status: {resp.status_code}", style="bold red")
        except Exception as e:
            self.log(f"❌ Handshake failed: {e}", style="bold red")
        return False

    def login(self) -> bool:
        """Authenticates with Cats vs Zombies backend API."""
        if not self.init_data:
            if not self.fetch_fresh_init_data():
                return False

        self.log("📡 Authenticating with Cats vs Zombies API...", style="cyan")
        body = {
            "initData": self.init_data,
            "startParam": None,
            "lang": "en"
        }
        try:
            resp = self.session.post(LOGIN_ENDPOINT, json=body, headers=self.default_headers, timeout=20)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("success") and "user" in data:
                    u = data["user"]
                    self.user_id = u.get("_id")
                    self.username = u.get("telegramName", "Player")
                    self.gold = u.get("gold", 0)
                    self.gems = u.get("gems", 0)
                    if self.start_gold == 0:
                        self.start_gold = self.gold
                    if self.start_gems == 0:
                        self.start_gems = self.gems

                    self.log(f"🎉 Login successful! Welcome Commander [bold yellow]{self.username}[/] | Gold: {self.gold} | Gems: {self.gems}", style="bold green")
                    return True
                elif data.get("error") == "maintenance":
                    self.log("⚠️ Game is currently undergoing maintenance. Waiting...", style="yellow")
                    time.sleep(30)
                    return False
            elif resp.status_code in [401, 403]:
                self.log("⚠️ Token expired or invalid. Refetching session...", style="yellow")
                self.init_data = None
                return False
        except Exception as e:
            self.log(f"⚠️ Login request error: {e}", style="bold red")
        return False

    def sync_home_status(self) -> bool:
        """Fetches home base state, wall integrity, cooldowns, and active cat squad."""
        if not self.init_data:
            return False

        body = {"initData": self.init_data}
        try:
            resp = self.session.post(HOME_STATUS_ENDPOINT, json=body, headers=self.default_headers, timeout=20)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("success"):
                    self.gold = data.get("gold", self.gold)
                    self.gems = data.get("gems", self.gems)
                    self.equipped_cats = data.get("cats", [])

                    w = data.get("wave", {})
                    self.current_wave = w.get("currentWave", self.current_wave)
                    self.wall_hp = w.get("wallHp", 0)
                    self.wall_max_hp = w.get("wallMaxHp", 1000)

                    # Cooldowns
                    cd_ms = w.get("cooldownRemainingMs") or 0
                    if cd_ms > 0:
                        self.cooldown_ends_at = max(self.cooldown_ends_at, time.time() + (cd_ms / 1000.0))

                    rep_ms = w.get("repairRemainingMs") or 0
                    if rep_ms > 0:
                        self.repair_ends_at = max(self.repair_ends_at, time.time() + (rep_ms / 1000.0))

                    # Daily farming caps
                    d = w.get("daily", {})
                    self.daily_gold_farmed = d.get("goldFarmed", 0)
                    self.daily_gold_cap = d.get("goldCap", 0)
                    self.daily_gems_farmed = d.get("gemsFarmed", 0)
                    self.daily_gems_cap = d.get("gemsCap", 0)

                    self.last_status_sync = time.time()
                    return True
                elif data.get("error") == "maintenance":
                    self.log("⚠️ Game under maintenance. Cooling down...", style="yellow")
                    time.sleep(30)
            elif resp.status_code in [401, 403]:
                self.log("⚠️ Auth invalid in status sync. Refetching session...", style="yellow")
                self.init_data = None
        except Exception as e:
            self.log(f"⚠️ Status sync error: {e}", style="red")
        return False

    def repair_wall(self) -> bool:
        """Dispatches fortress wall repair request."""
        self.action_status = "Repairing Wall..."
        self.log(f"🛡️ Wall breached (HP: {self.wall_hp}/{self.wall_max_hp})! Requesting fortress wall repair...", style="bold yellow")
        body = {"initData": self.init_data}
        try:
            resp = self.session.post(WAVE_REPAIR_ENDPOINT, json=body, headers=self.default_headers, timeout=20)
            data = resp.json()
            if data.get("success"):
                rep_ms = data.get("repairRemainingMs", 15000)
                self.repair_ends_at = time.time() + (rep_ms / 1000.0)
                self.wall_repairs += 1
                self.log(f"🔨 Wall repair commenced! Duration: {rep_ms / 1000.0:.1f}s", style="green")
                self.emit_telemetry("repair_started")
                return True
            elif data.get("error") == "wall_already_full":
                self.log("🛡️ Wall already intact.", style="green")
                self.sync_home_status()
                return True
            else:
                self.log(f"⚠️ Wall repair warning: {data.get('error')}", style="yellow")
        except Exception as e:
            self.log(f"❌ Wall repair request error: {e}", style="bold red")
        return False

    def play_wave(self) -> bool:
        """Executes a wave combat simulation against invading zombies."""
        self.action_status = f"Battling Wave #{self.current_wave}"
        self.log(f"⚔️ Engaging Wave #{self.current_wave} (Squad: {len(self.equipped_cats)} cats | Wall: {self.wall_hp}/{self.wall_max_hp})...", style="bold cyan")
        self.emit_telemetry("wave_start")

        body = {"initData": self.init_data}
        try:
            resp = self.session.post(WAVE_PLAY_ENDPOINT, json=body, headers=self.default_headers, timeout=30)
            data = resp.json()

            if data.get("success"):
                # Wave cleared or finished
                res_type = data.get("result", "cleared")
                wave_num = data.get("wave", self.current_wave)
                next_wave = data.get("nextWave", wave_num + 1)
                gold_earned = data.get("goldEarned", 0)
                gems_earned = data.get("gemsEarned", 0)
                self.gold = data.get("gold", self.gold + gold_earned)
                self.gems = data.get("gems", self.gems + gems_earned)
                self.wall_hp = data.get("wallHp", self.wall_hp)
                self.wall_max_hp = data.get("wallMaxHp", self.wall_max_hp)

                kills = data.get("kills", 0)
                enemies = data.get("enemies", kills)
                cd_ms = data.get("cooldownMs", 45000)
                self.cooldown_ends_at = time.time() + (cd_ms / 1000.0)

                # Update daily farm
                d = data.get("daily", {})
                if d:
                    self.daily_gold_farmed = d.get("goldFarmed", self.daily_gold_farmed)
                    self.daily_gems_farmed = d.get("gemsFarmed", self.daily_gems_farmed)

                if res_type == "cleared":
                    self.waves_cleared += 1
                    self.current_wave = next_wave
                    self.log(
                        f"🏆 [bold green]VICTORY! Wave #{wave_num} Cleared![/] "
                        f"Kills: {kills}/{enemies} | Gold: +{gold_earned} (Total: {self.gold}) | Gems: +{gems_earned} (Total: {self.gems}) | "
                        f"Wall: {self.wall_hp}/{self.wall_max_hp} | Cooldown: {cd_ms/1000:.0f}s",
                        style="green"
                    )
                else:
                    self.waves_lost += 1
                    self.log(f"💥 Wave #{wave_num} Ended ({res_type}) | Wall: {self.wall_hp}/{self.wall_max_hp}", style="yellow")

                self.action_status = f"Wave #{wave_num} Cleared (+{gold_earned}G)"
                self.emit_telemetry("wave_result", {
                    "last_wave_cleared": wave_num,
                    "last_gold_earned": gold_earned,
                    "last_gems_earned": gems_earned
                })

                # Human-like simulated combat delay (2.5 - 4.5 seconds)
                time.sleep(random.uniform(2.5, 4.5))
                return True

            # Error cases handled identically to menu_home.js
            err = data.get("error")
            if err == "wave_on_cooldown":
                rem_ms = data.get("remainingMs", 15000)
                self.cooldown_ends_at = time.time() + (rem_ms / 1000.0)
                self.log(f"⏳ Wave on cooldown. Waiting {rem_ms / 1000.0:.1f}s...", style="dim")
                return False
            elif err == "wall_repairing":
                rem_ms = data.get("remainingMs", 15000)
                self.repair_ends_at = time.time() + (rem_ms / 1000.0)
                self.log(f"🔨 Wall currently repairing. Waiting {rem_ms / 1000.0:.1f}s...", style="dim")
                return False
            elif err == "wall_destroyed":
                self.repair_wall()
                return False
            elif err == "no_cats_equipped":
                self.log("⚠️ No cats equipped in squad! Please equip a cat.", style="bold red")
                time.sleep(10)
                return False
            else:
                self.log(f"⚠️ Wave play warning: {err}", style="yellow")
                time.sleep(5)
                return False
        except Exception as e:
            self.log(f"❌ Wave play exception: {e}", style="bold red")
            time.sleep(5)
            return False

    def check_and_claim_tasks(self):
        """Claims daily checkin, goodmorning, displayname, and milestone achievements."""
        now = time.time()
        if now - self.last_task_check < 300:  # Check every 5 minutes
            return

        self.last_task_check = now
        self.log("📋 Checking daily tasks and milestone achievements...", style="cyan")
        body = {"initData": self.init_data}
        try:
            resp = self.session.post(TASKS_STATUS_ENDPOINT, json=body, headers=self.default_headers, timeout=20)
            if resp.status_code != 200:
                return

            data = resp.json()
            daily = data.get("daily", {})

            # 1. Daily check-in
            checkin = daily.get("checkin", {})
            if not checkin.get("claimed"):
                r_c = self.session.post(TASKS_CLAIM_DAILY, json={"initData": self.init_data, "key": "checkin"}, headers=self.default_headers, timeout=15)
                c_json = r_c.json()
                if c_json.get("success"):
                    self.gold = c_json.get("gold", self.gold)
                    self.log(f"🎁 Daily Check-in Claimed! Reward: +{c_json.get('reward', 50)} Gold", style="bold green")

            # 2. Daily goodmorning
            gm = daily.get("goodmorning", {})
            if not gm.get("claimed"):
                r_gm = self.session.post(TASKS_CLAIM_DAILY, json={"initData": self.init_data, "key": "goodmorning"}, headers=self.default_headers, timeout=15)
                gm_json = r_gm.json()
                if gm_json.get("success"):
                    self.gold = gm_json.get("gold", self.gold)
                    self.log(f"☀️ Daily Good Morning Claimed! Reward: +{gm_json.get('reward', 10)} Gold", style="bold green")

            # 3. Display name
            dn = daily.get("displayname", {})
            if dn.get("eligible") and not dn.get("claimed"):
                r_dn = self.session.post(TASKS_CLAIM_DAILY, json={"initData": self.init_data, "key": "displayname"}, headers=self.default_headers, timeout=15)
                dn_json = r_dn.json()
                if dn_json.get("success"):
                    self.gold = dn_json.get("gold", self.gold)
                    self.log(f"🏷️ Display Name Task Claimed! Reward: +{dn_json.get('reward', 20)} Gold", style="bold green")

            # 4. Daily chest
            dc = data.get("dailyChest", {})
            if dc.get("ready") and not dc.get("claimed"):
                r_chest = self.session.post(TASKS_CLAIM_CHEST, json={"initData": self.init_data}, headers=self.default_headers, timeout=15)
                chest_json = r_chest.json()
                if chest_json.get("success"):
                    self.gold = chest_json.get("gold", self.gold)
                    self.log(f"📦 Daily Chest Opened! Reward: +{chest_json.get('reward', 0)} Gold", style="bold green")

            # 5. Milestones
            milestones = data.get("milestones", [])
            for m in milestones:
                if m.get("ready") and not m.get("claimed"):
                    mid = m.get("id")
                    r_m = self.session.post(TASKS_CLAIM_MILESTONE, json={"initData": self.init_data, "id": mid}, headers=self.default_headers, timeout=15)
                    m_json = r_m.json()
                    if m_json.get("success"):
                        self.gold = m_json.get("gold", self.gold)
                        self.log(f"⭐ Milestone '{m.get('title', mid)}' Claimed! Reward: +{m_json.get('reward', 0)} Gold", style="bold green")
        except Exception as e:
            self.log(f"⚠️ Tasks check error: {e}", style="dim")

    def process_ipc_commands(self):
        """Reads remote control directives dispatched from the central web dashboard."""
        if not os.path.exists(CMD_FILE):
            return

        try:
            with open(CMD_FILE, "r", encoding="utf-8") as f:
                cmd = json.load(f)
            os.remove(CMD_FILE)

            action = cmd.get("action")
            if action == "repair_wall":
                self.log("🕹️ [IPC Command] Immediate Wall Repair requested via Dashboard!", style="bold magenta")
                self.repair_wall()
            elif action == "claim_tasks":
                self.log("🕹️ [IPC Command] Immediate Tasks Claim requested via Dashboard!", style="bold magenta")
                self.last_task_check = 0
                self.check_and_claim_tasks()
            elif action == "pause":
                self.is_paused = True
                self.action_status = "Paused"
                self.log("⏸️ [IPC Command] Autoplay paused via Dashboard.", style="yellow")
            elif action == "resume":
                self.is_paused = False
                self.log("▶️ [IPC Command] Autoplay resumed via Dashboard.", style="green")
        except Exception:
            pass

    def run_forever(self):
        """Main autonomous execution loop."""
        console.print(Panel(
            Text.from_markup(
                f"[bold magenta]CATS VS ZOMBIES AUTONOMOUS SENTINEL[/]\n"
                f"[cyan]Target Bot:[/] @{self.bot_username} | [cyan]Backend:[/] {API_BASE_URL}\n"
                f"[green]Features:[/] Infinite Wave Auto-Combat, Auto Wall Repair, Task Claimer, Rich Observability"
            ),
            border_style="magenta"
        ))

        # Initial Handshake & Login
        while not self.login():
            self.log("⚠️ Initial login failed. Retrying in 10 seconds...", style="yellow")
            time.sleep(10)

        # Initial Status Sync
        self.sync_home_status()
        self.check_and_claim_tasks()
        self.emit_telemetry("initial_sync")

        last_hud_print = 0

        while True:
            try:
                # 1. Process dashboard IPC commands
                self.process_ipc_commands()

                if self.is_paused:
                    time.sleep(1)
                    continue

                # 2. Check and claim daily tasks
                self.check_and_claim_tasks()

                now = time.time()

                # Periodic status resync (every 60s)
                if now - self.last_status_sync > 60:
                    self.sync_home_status()
                    self.emit_telemetry("sync")

                # 3. Check Wall Integrity
                if self.wall_hp <= 0:
                    if now >= self.repair_ends_at:
                        # Wall is breached and not currently repairing
                        self.repair_wall()
                    else:
                        rem_s = int(self.repair_ends_at - now)
                        self.action_status = f"Repairing Wall ({rem_s}s)"
                        if now - last_hud_print >= 5:
                            self.log(f"🔨 Wall repairing in progress: {rem_s}s remaining...", style="dim yellow")
                            last_hud_print = now
                            self.emit_telemetry("repair_tick")
                        time.sleep(1)
                        continue

                # 4. Check Cooldown
                if now < self.repair_ends_at:
                    rem_s = int(self.repair_ends_at - now)
                    self.action_status = f"Repairing Wall ({rem_s}s)"
                    if now - last_hud_print >= 5:
                        self.log(f"🔨 Wall repairing: {rem_s}s remaining...", style="dim yellow")
                        last_hud_print = now
                        self.emit_telemetry("repair_tick")
                    time.sleep(1)
                    continue

                if now < self.cooldown_ends_at:
                    rem_s = int(self.cooldown_ends_at - now)
                    self.action_status = f"Wave Cooldown ({rem_s}s)"
                    if now - last_hud_print >= 5:
                        self.log(f"⏳ Wave Cooldown: {rem_s}s remaining before next wave...", style="dim cyan")
                        last_hud_print = now
                        self.emit_telemetry("cooldown_tick")
                    time.sleep(1)
                    continue

                # 5. Ready for Battle!
                self.action_status = f"Engaging Wave #{self.current_wave}..."
                self.play_wave()

            except KeyboardInterrupt:
                self.log("🛑 Engine stopped by user.", style="bold red")
                break
            except Exception as e:
                self.log(f"❌ Unexpected loop exception: {e}", style="bold red")
                time.sleep(5)


if __name__ == "__main__":
    engine = CatVSZombieEngine()
    engine.run_forever()
