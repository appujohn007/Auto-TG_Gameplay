"""
Rignite Human-Like Automated Tapping & Telemetry Engine (Infinite Auto-Pilot)
----------------------------------------------------------------------------
Faithfully reverse-engineered from Rignite web app client:
- Target Bot: @RigniteBot (t.me/RigniteBot/app)
- API Base: https://api.rignite.app
- Session Maker: https://tg-game-sessionmaker.onrender.com

Autonomous Infinite Loop Architecture:
1. Auto-Handshake: Automatically fetches fresh Telegram initData via SessionMaker and logs in.
2. Dynamic Tap Bursts: Taps in human-like batches (5-14 taps) with Gaussian jitter (180ms-320ms).
3. Alternating Pole Simulation: Alternates Left and Right poles (~88%) with occasional organic slips.
4. Telemetry Reporting: Full mathematical calculation of frontend go() tapfp algorithm to /client-log.
5. Energy & Recharge Lifecycle:
   - When energy depletes (<= 15):
     a) Checks and consumes daily instant energy refill (`/boost/full-energy`) if available.
     b) When boosts are exhausted, enters real human sleep/recharge mode with a live countdown HUD.
6. Rate-Limit Immunity: Listens to x-ratelimit-reset on 429 and applies exponential backoff.
"""

import sys
import os
import time
import math
import random
import uuid
import json
import urllib.parse
from datetime import datetime, timezone
from typing import Optional, Dict, Any, Tuple

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
from rich.live import Live
from rich.text import Text

console = Console(legacy_windows=False)

# Endpoints
API_BASE_URL = "https://api.rignite.app"
AUTH_ENDPOINT = f"{API_BASE_URL}/auth"
TAP_ENDPOINT = f"{API_BASE_URL}/tap"
ME_ENDPOINT = f"{API_BASE_URL}/me"
BOOST_FULL_ENERGY = f"{API_BASE_URL}/boost/full-energy"
COLLECT_SHIFT_ENDPOINT = f"{API_BASE_URL}/shift/collect"
COLLECT_BUILDING_ENDPOINT = f"{API_BASE_URL}/shift/collect-building"
SANTRAL_TOPLA_ENDPOINT = f"{API_BASE_URL}/santral/topla"
DAILY_CLAIM_ENDPOINT = f"{API_BASE_URL}/daily/claim"
SURPRISE_CLAIM_ENDPOINT = f"{API_BASE_URL}/surprise/claim"
FRIENDS_CLAIM_ENDPOINT = f"{API_BASE_URL}/friends/claim"
AD_INTENT_ENDPOINT = f"{API_BASE_URL}/ad/intent"
AD_COMPLETE_ENDPOINT = f"{API_BASE_URL}/ad/complete"
CLIENT_LOG_ENDPOINT = f"{API_BASE_URL}/client-log"
SESSION_MAKER_URL = "https://tg-game-sessionmaker.onrender.com/api/get-init-data"

DEFAULT_BOT = "RigniteBot"
DEFAULT_SHORT_NAME = "app"

# Command IPC directory for dashboard interactions
IPC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
RIGNITE_CMD_FILE = os.path.join(IPC_DIR, "rignite_cmd.json")



def create_robust_session() -> requests.Session:
    """Creates a requests session configured with retries and DNS tolerance."""
    session = requests.Session()
    retry_strategy = Retry(
        total=4,
        backoff_factor=1.5,
        status_forcelist=[500, 502, 503, 504],
        raise_on_status=False
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def to_base36(num: int) -> str:
    """Matches JavaScript unsigned integer .toString(36)."""
    chars = "0123456789abcdefghijklmnopqrstuvwxyz"
    if num == 0:
        return "0"
    digits = []
    while num > 0:
        digits.append(chars[num % 36])
        num //= 36
    return "".join(reversed(digits))


class HumanTapTelemetry:
    """
    Simulates real human touch telemetry matching the client's internal `go()` tapfp algorithm:
    - Tracks intervals `ivort` (mean tap interval) and coefficient of variation `ivcv`
    - Generates 2D tap spatial jitter `xystd` across both poles ('L' and 'R' buttons)
    - Enforces organic alternation logic (avoiding repetitive single-pole bot triggers)
    - Computes 32-bit signature hash `imza` using rolling prime multiplication (x31 >>> 0)
    """

    def __init__(self):
        self.ts = []       # Timestamps
        self.xs = []       # clientX coordinates
        self.ys = []       # clientY coordinates
        self.same = 0      # Same pole tap counter
        self.gonderim = 0  # Sent reports counter (capped at 4 in frontend)
        self.last_pole = "L"
        self.virtual_clock = time.time() * 1000.0

    def record_tap(self) -> Tuple[float, float, float, bool]:
        """
        Simulates a human physical touch event on the web app.
        Models natural single-finger fast tapping on the '+' pole (~74% on '+').
        Matches your real telemetry: ivort ~160-200ms, same ~0.74, xystd ~55.
        """
        if random.random() < 0.06:
            interval = random.uniform(240.0, 390.0)
        else:
            interval = max(95.0, min(280.0, random.gauss(165.0, 28.0)))
        
        self.virtual_clock += interval

        is_same = random.random() < 0.74
        if is_same:
            pole = self.last_pole
            self.same += 1
        else:
            pole = "R" if self.last_pole == "L" else "L"
            self.last_pole = pole

        center_x = 110.0 if pole == "L" else 280.0
        center_y = 620.0
        x = round(random.gauss(center_x, 14.0), 1)
        y = round(random.gauss(center_y, 12.0), 1)

        self.ts.append(self.virtual_clock)
        self.xs.append(x)
        self.ys.append(y)

        return self.virtual_clock, x, y, is_same

    def should_report(self) -> bool:
        return len(self.ts) >= 500 and self.gonderim < 4

    def build_report_payload(self) -> Optional[str]:
        """Replicates exact frontend client calculation in index-BCVJTezd.js."""
        if len(self.ts) < 500:
            return None

        e = []
        for t in range(1, len(self.ts)):
            n = self.ts[t] - self.ts[t - 1]
            if 0 < n < 2000:
                e.append(n)

        t_mean = sum(e) / max(1, len(e))
        variance_n = sum((item - t_mean) ** 2 for item in e) / max(1, len(e))
        n_std = math.sqrt(variance_n)

        r_mean_x = sum(self.xs) / len(self.xs)
        i_mean_y = sum(self.ys) / len(self.ys)

        var_xy = sum(
            (self.xs[idx] - r_mean_x) ** 2 + (self.ys[idx] - i_mean_y) ** 2
            for idx in range(len(self.xs))
        ) / len(self.xs)
        a_xystd = math.sqrt(var_xy)

        o = 0
        for idx in range(min(64, len(e))):
            val = round(e[idx] / 20.0)
            o = ((o * 31) + val) & 0xFFFFFFFF

        ivcv = n_std / max(1.0, t_mean)
        same_ratio = self.same / len(self.ts)
        imza_str = to_base36(o)

        msg = (
            f"tapfp: n={len(self.ts)} "
            f"ivort={round(t_mean)} "
            f"ivcv={ivcv:.3f} "
            f"xystd={a_xystd:.1f} "
            f"same={same_ratio:.2f} "
            f"imza={imza_str}"
        )

        self.gonderim += 1
        self.ts.clear()
        self.xs.clear()
        self.ys.clear()
        self.same = 0

        return msg


class RigniteClient:
    def __init__(self, bot_username: str = DEFAULT_BOT):
        self.bot_username = bot_username
        self.session_token: Optional[str] = None
        self.init_data: Optional[str] = None
        self.telemetry = HumanTapTelemetry()
        self.http = create_robust_session()

        # Real-time state
        self.user_info: Dict[str, Any] = {}
        self.coins: int = 0
        self.start_coins: Optional[int] = None
        self.session_start_time: float = time.time()
        self.energy: int = 1000
        self.max_energy: int = 1000
        self.battery_energy: int = 0
        self.battery_cap: int = 0
        self.full_energy_left: int = 0
        self.tap_mult: int = 1
        self.session_taps: int = 0
        self.total_accepted: int = 0
        self.batches_sent: int = 0
        self.cycle_count: int = 1
        self.last_ad_energy_time: float = 0.0  # Tracks when ad energy refill was last consumed
        self.burst_target: int = 0             # Target ads in current burst (e.g. 2-5)
        self.burst_count: int = 0              # Ads watched so far in current burst
        self.colony_coins_total: int = 0       # Total colony coins collected separately
        self.last_colony_collect_time: float = 0.0  # Timestamp of last colony collection
        self.next_colony_collect_interval: float = float(random.randint(600, 1800))  # Natural 10-30 min gap
        self.last_daily_claim_time: float = 0.0     # Timestamp of last daily reward check
        self.last_surprise_claim_time: float = 0.0  # Timestamp of last surprise box check
        self.last_santral_collect_time: float = 0.0 # Timestamp of last power plant check
        self.last_friends_claim_time: float = 0.0   # Timestamp of last friends commission check
        self.next_action_eta: str = "Tapping Active"
        self.last_status: str = "Initialized"
        self.recent_logs: list = []
        self._last_telemetry_emit: float = 0.0

    def emit_telemetry(self, event_type: str = "tap_sync", extra: Optional[Dict[str, Any]] = None):
        """Emits structured JSON metrics for AutoPlayer supervisor without breaking HUD/console."""
        try:
            # Calculate rolling real rate metrics
            elapsed = max(1.0, time.time() - self.session_start_time)
            coins_delta = self.coins - (self.start_coins if self.start_coins is not None else self.coins)
            earned_positive = max(0, coins_delta)
            rate_cps = earned_positive / elapsed
            cadence = (self.session_taps / elapsed) if elapsed > 0 else 0.0

            payload = {
                "game": "rignite",
                "type": event_type,
                "user": self.user_info.get("username") or self.user_info.get("first_name", "Player"),
                "coins": self.coins,
                "start_coins": self.start_coins,
                "coins_delta": coins_delta,
                "rate_cps": rate_cps,
                "rate_cpm": int(rate_cps * 60.0),
                "energy": self.energy,
                "max_energy": self.max_energy,
                "battery_energy": self.battery_energy,
                "battery_cap": self.battery_cap,
                "full_energy_left": self.full_energy_left,
                "ad_energy_left": self.ad_energy_left,
                "ad_boost_left": self.ad_boost_left,
                "tap_mult": self.tap_mult,
                "action_status": self.last_status,
                "next_action": self.next_action_eta,
                "session_taps": self.session_taps,
                "tap_cadence": cadence,
                "total_accepted": self.total_accepted,
                "cycle_count": self.cycle_count,
                "batches_sent": self.batches_sent,
                "colony_coins_total": self.colony_coins_total,
                "timestamp": int(time.time() * 1000)
            }
            if extra:
                payload.update(extra)
            if self.init_data:
                payload["web_app_url"] = f"https://app.rignite.app/#tgWebAppData={urllib.parse.quote(self.init_data)}&tgWebAppVersion=8.0&tgWebAppPlatform=weba"
            # Flush directly to stdout
            sys.stdout.write(f"\n[AUTOPLAYER_METRIC] {json.dumps(payload)}\n")
            sys.stdout.flush()
        except Exception:
            pass

    def log(self, text: str, style: str = "white"):
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.recent_logs.append((timestamp, text, style))
        if len(self.recent_logs) > 6:
            self.recent_logs.pop(0)
        # If not running in an interactive terminal (e.g. supervised pipe), stream log to console so supervisor captures it
        if not sys.stdout.isatty():
            console.print(f"[Rignite] {text}")

    def fetch_fresh_init_data(
        self,
        tg_url: Optional[str] = None,
        start_param: str = "",
        session_string: Optional[str] = None
    ) -> str:
        """Requests fresh signed Telegram WebApp initData from tg-game-sessionmaker."""
        self.last_status = "Contacting sessionmaker..."
        self.log(f"Requesting Telegram auth for [bold cyan]@{self.bot_username}[/]", "cyan")

        payload: Dict[str, Any] = {"platform": "android"}

        if tg_url:
            payload["tg_url"] = tg_url
        else:
            payload["bot"] = self.bot_username
            payload["short_name"] = DEFAULT_SHORT_NAME
            if start_param:
                payload["start_param"] = start_param

        if session_string:
            payload["session_string"] = session_string

        max_attempts = 4
        last_error = None

        for attempt in range(1, max_attempts + 1):
            try:
                resp = self.http.post(SESSION_MAKER_URL, json=payload, timeout=30)
                resp.raise_for_status()
                data = resp.json()

                if not data.get("success", False) and "initData" not in data:
                    raise ValueError(f"Sessionmaker response error: {data}")

                self.init_data = data.get("initData") or data.get("init_data")
                if not self.init_data:
                    raise ValueError("Sessionmaker returned empty initData")

                self.log(f"Obtained fresh signed initData ({len(self.init_data)} chars)", "green")
                return self.init_data
            except Exception as e:
                last_error = e
                self.log(f"Waking sessionmaker (Attempt {attempt}/{max_attempts})...", "yellow")
                time.sleep(2.0 * attempt)

        self.log(f"SessionMaker error: {last_error}", "red")
        raise last_error

    def authenticate(self) -> str:
        """Authenticates with api.rignite.app using exact client protocol."""
        if not self.init_data:
            self.fetch_fresh_init_data()

        self.last_status = "Authenticating with api.rignite.app..."
        today_fp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        dev_id = str(uuid.uuid4())

        headers = self._get_auth_headers()
        headers["x-init-data"] = self.init_data

        payload = {
            "lang": "en",
            "dev": dev_id,
            "fp": today_fp
        }

        resp = self.http.post(AUTH_ENDPOINT, json=payload, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        token = data.get("sessionId")
        if not token:
            raise ValueError(f"Auth response missing sessionId: {data}")

        self.session_token = token
        self.user_info = data.get("user", {})
        self.coins = self.user_info.get("coins", 0)
        if self.start_coins is None:
            self.start_coins = self.coins
            self.session_start_time = time.time()
        self.energy = self.user_info.get("energy", 1000)
        self.max_energy = self.user_info.get("maxEnergy", 1000)
        self.battery_energy = self.user_info.get("batteryEnergy", 0)
        self.battery_cap = self.user_info.get("batteryCap", 0)
        self.full_energy_left = self.user_info.get("fullEnergyLeft", 0)
        self.ad_energy_left = self.user_info.get("adEnergyLeft", 0)
        self.ad_boost_left = self.user_info.get("adBoostLeft", 0)
        self.boost_until = self.user_info.get("boostUntil", 0)
        user_name = self.user_info.get("username") or self.user_info.get("first_name", "Player")

        self.log(f"Logged in as: [bold yellow]@{user_name}[/]", "green")
        self.log(f"Balance: [yellow]{self.coins:,}[/] | Energy: [green]{self.energy}/{self.max_energy}[/] | Boosts: [cyan]{self.full_energy_left}[/] | Ad Energy: [cyan]{self.ad_energy_left}[/]", "white")
        self.emit_telemetry("auth_success")
        return token

    def refresh_me(self):
        """Syncs the player's profile and current energy/battery state."""
        headers = self._get_auth_headers()
        try:
            resp = self.http.post(ME_ENDPOINT, json={}, headers=headers, timeout=12)
            if resp.status_code == 200:
                data = resp.json()
                self.coins = data.get("coins", self.coins)
                self.energy = data.get("energy", self.energy)
                self.max_energy = data.get("maxEnergy", self.max_energy)
                self.battery_energy = data.get("batteryEnergy", self.battery_energy)
                self.battery_cap = data.get("batteryCap", self.battery_cap)
                self.full_energy_left = data.get("fullEnergyLeft", self.full_energy_left)
                self.ad_energy_left = data.get("adEnergyLeft", self.ad_energy_left)
                self.ad_boost_left = data.get("adBoostLeft", self.ad_boost_left)
                self.boost_until = data.get("boostUntil", self.boost_until)
                return data
        except Exception as e:
            self.log(f"Profile refresh error: {e}", "red")
        return None

    def check_manual_commands(self):
        """Checks for pending manual user commands sent from the dashboard."""
        if not os.path.exists(RIGNITE_CMD_FILE):
            return
        try:
            with open(RIGNITE_CMD_FILE, "r", encoding="utf-8") as f:
                cmd_data = json.load(f)
            # Remove file immediately so command is consumed only once
            try:
                os.remove(RIGNITE_CMD_FILE)
            except Exception:
                pass
            
            action = cmd_data.get("action")
            if action == "watch_ad_refill":
                self.log("Manual Refill Ad command triggered from Dashboard!", "bold cyan")
                self.last_status = "Manual Ad Refill requested via Dashboard..."
                self.next_action_eta = "Watching Ad..."
                self.emit_telemetry("ad_manual_triggered", {"action_status": self.last_status, "next_action": self.next_action_eta})
                if self.simulate_rewarded_ad("full_energy"):
                    self.log(f"Manual Ad Refill complete! Energy: {self.energy}/{self.max_energy}", "bold green")
                else:
                    self.log("Manual Ad Refill could not be completed (cooldown or rejected).", "yellow")
        except Exception as e:
            try:
                if os.path.exists(RIGNITE_CMD_FILE):
                    os.remove(RIGNITE_CMD_FILE)
            except Exception:
                pass
            self.log(f"Error checking manual command: {e}", "red")

    def simulate_rewarded_ad(self, ad_type: str = "full_energy") -> bool:
        """
        Executes a human-like 2-step ad handshake:
        1. POST /ad/intent with ad type ('full_energy' or 'boost')
        2. Waits realistic viewing duration (16s - 26s)
        3. POST /ad/complete with natural ms and tapGap parameters
        """
        headers = self._get_auth_headers()
        try:
            intent_resp = self.http.post(AD_INTENT_ENDPOINT, json={"type": ad_type}, headers=headers, timeout=12)
            if intent_resp.status_code != 200:
                return False
            
            intent_data = intent_resp.json()
            if not intent_data.get("ok", True) and intent_data.get("cooldown"):
                cooldown_sec = intent_data.get("cooldown", 0)
                self.log(f"Ad '{ad_type}' cooldown: {cooldown_sec}s remaining", "dim yellow")
                return False

            # Human-like ad viewing duration (16 - 25 seconds)
            view_seconds = random.randint(16, 25)
            self.log(f"Watching rewarded ad for {ad_type} ({view_seconds}s)...", "cyan")
            self.last_status = f"Watching {ad_type} ad ({view_seconds}s)..."
            self.next_action_eta = f"Ad finishes in {view_seconds}s"
            self.emit_telemetry("ad_watch_start", {"action_status": self.last_status, "next_action": self.next_action_eta})
            for sec in range(view_seconds, 0, -1):
                self.last_status = f"Watching {ad_type} ad ({sec}s remaining)..."
                self.next_action_eta = f"Ad finishes in {sec}s"
                if sec % 3 == 0 or sec <= 2:
                    self.emit_telemetry("ad_watching", {"action_status": self.last_status, "next_action": self.next_action_eta})
                time.sleep(1.0)

            # Build realistic completion payload
            elapsed_ms = view_seconds * 1000 + random.randint(120, 850)
            tap_gap = random.randint(35000, 68000)
            complete_payload = {
                "network": "warhold",
                "ymid": None,
                "ms": elapsed_ms,
                "tapGap": tap_gap
            }

            comp_resp = self.http.post(AD_COMPLETE_ENDPOINT, json=complete_payload, headers=headers, timeout=12)
            if comp_resp.status_code == 200:
                self.log(f"✅ Ad reward claimed for {ad_type}!", "bold green")
                if ad_type == "full_energy":
                    self.last_ad_energy_time = time.time()
                self.refresh_me()
                self.emit_telemetry("ad_reward_claimed", {"action_status": f"Claimed {ad_type} ad reward", "next_action": "Resuming taps"})
                return True
            else:
                self.log(f"Ad completion rejected (HTTP {comp_resp.status_code})", "yellow")
        except Exception as e:
            self.log(f"Ad simulation error: {e}", "red")
        return False

    def trigger_full_energy_boost(self) -> bool:
        """Consumes a daily free instant energy recharge booster."""
        if self.full_energy_left <= 0:
            return False

        headers = self._get_auth_headers()
        try:
            resp = self.http.post(BOOST_FULL_ENERGY, json={}, headers=headers, timeout=12)
            if resp.status_code == 200:
                data = resp.json()
                self.energy = data.get("energy", self.max_energy)
                self.full_energy_left = data.get("fullEnergyLeft", 0)
                self.last_status = "Instant full energy refill activated"
                self.next_action_eta = "Ready to tap"
                self.log(f"⚡ Instant full energy refill activated! ({self.full_energy_left} left)", "bold green")
                self.emit_telemetry("boost_refill", {"action_status": self.last_status, "next_action": self.next_action_eta})
                return True
        except Exception as e:
            self.log(f"Booster activation error: {e}", "red")
        return False

    def collect_colony_income(self) -> int:
        """
        Collects accumulated colony passive income from POST /shift/collect.
        Maintains start_coins baseline so passive collection does not distort
        active tap velocity (coins/s, coins/min) or net tap delta.
        Handles BATTERY_FULL and ENERJI_DOLU conditions gracefully.
        """
        # Proactively check battery energy level before collecting
        if self.battery_cap > 0 and self.battery_energy >= self.battery_cap:
            self.log(f"🔋 Battery capacity full ({self.battery_energy}/{self.battery_cap}). Skipping shift collection until energy is consumed.", "dim yellow")
            return 0

        headers = self._get_auth_headers()
        try:
            resp = self.http.post(COLLECT_SHIFT_ENDPOINT, json={}, headers=headers, timeout=12)
            if resp.status_code == 200:
                data = resp.json()
                collected = data.get("collected", 0)
                new_coins = data.get("coins", self.coins)
                
                # Update coin balance
                self.coins = new_coins

                # Adjust start_coins baseline so tap velocity & tap delta ignore colony income
                if collected > 0 and self.start_coins is not None:
                    self.start_coins += collected
                    self.colony_coins_total += collected
                    self.log(f"🏛️ Collected {collected:,} colony coins (Total colony: {self.colony_coins_total:,})", "bold cyan")
                    self.emit_telemetry("colony_collect", {
                        "collected_now": collected,
                        "action_status": f"Collected {collected:,} colony coins"
                    })
                return collected
            else:
                try:
                    err_json = resp.json()
                    err_code = err_json.get("error", "")
                    if err_code in ("BATTERY_FULL", "ENERJI_DOLU"):
                        self.log(f"🔋 Colony collection paused: {err_code} (will retry naturally after battery drain/tapping)", "dim yellow")
                        return 0
                except Exception:
                    pass
                if resp.status_code not in (400, 409):
                    self.log(f"Colony collect status HTTP {resp.status_code}", "dim yellow")
        except Exception as e:
            self.log(f"Colony collect error: {e}", "dim red")
        return 0

    def claim_daily_reward(self) -> bool:
        """Claims daily login streak reward from POST /daily/claim."""
        headers = self._get_auth_headers()
        try:
            resp = self.http.post(DAILY_CLAIM_ENDPOINT, json={}, headers=headers, timeout=12)
            if resp.status_code == 200:
                data = resp.json()
                coins_reward = data.get("coins", 0) or data.get("reward", 0)
                if coins_reward and self.start_coins is not None:
                    self.start_coins += coins_reward
                    self.coins += coins_reward
                streak = data.get("streak") or data.get("dailyStreak", "")
                self.log(f"🔥 Daily Login Reward claimed successfully! Streak: {streak}", "bold green")
                self.emit_telemetry("daily_claimed", {"action_status": "Daily reward claimed"})
                return True
            else:
                try:
                    err = resp.json().get("error", "")
                    if err in ("BATTERY_FULL", "ENERJI_DOLU"):
                        self.log(f"Daily claim deferred: {err}", "dim yellow")
                except Exception:
                    pass
        except Exception as e:
            self.log(f"Daily claim error: {e}", "dim red")
        return False

    def claim_surprise_box(self) -> bool:
        """Claims mystery / surprise box bonus from POST /surprise/claim."""
        headers = self._get_auth_headers()
        try:
            resp = self.http.post(SURPRISE_CLAIM_ENDPOINT, json={}, headers=headers, timeout=12)
            if resp.status_code == 200:
                data = resp.json()
                reward = data.get("reward") or data.get("coins", 0)
                if isinstance(reward, int) and reward > 0 and self.start_coins is not None:
                    self.start_coins += reward
                    self.coins += reward
                self.log("🎁 Surprise / Mystery Box claimed successfully!", "bold magenta")
                self.emit_telemetry("surprise_claimed", {"action_status": "Surprise box claimed"})
                return True
            else:
                try:
                    err = resp.json().get("error", "")
                    if err in ("BATTERY_FULL", "ENERJI_DOLU"):
                        self.log(f"Surprise box deferred: {err}", "dim yellow")
                except Exception:
                    pass
        except Exception as e:
            self.log(f"Surprise claim error: {e}", "dim red")
        return False

    def collect_power_plant(self, no: int = 1) -> bool:
        """Collects reactor generation from power plant unit via POST /santral/topla."""
        if self.battery_cap > 0 and self.battery_energy >= self.battery_cap:
            return False

        headers = self._get_auth_headers()
        try:
            resp = self.http.post(SANTRAL_TOPLA_ENDPOINT, json={"no": no}, headers=headers, timeout=12)
            if resp.status_code == 200:
                data = resp.json()
                alinan = data.get("alinan", 0)
                self.log(f"⚡ Power Plant (Santral #{no}) generation collected: +{alinan} energy units", "bold cyan")
                self.refresh_me()
                return True
            else:
                try:
                    err = resp.json().get("error", "")
                    if err in ("BATTERY_FULL", "ENERJI_DOLU"):
                        self.log(f"Santral #{no} collect deferred: {err}", "dim yellow")
                except Exception:
                    pass
        except Exception as e:
            self.log(f"Santral collect error: {e}", "dim red")
        return False

    def claim_friends_commission(self) -> bool:
        """Collects friend referral commissions via POST /friends/claim."""
        headers = self._get_auth_headers()
        try:
            resp = self.http.post(FRIENDS_CLAIM_ENDPOINT, json={}, headers=headers, timeout=12)
            if resp.status_code == 200:
                data = resp.json()
                claimed = data.get("claimed", 0)
                if claimed > 0:
                    if self.start_coins is not None:
                        self.start_coins += claimed
                        self.coins += claimed
                    self.log(f"👥 Friends commission claimed: +{claimed:,} coins", "bold green")
                return True
        except Exception:
            pass
        return False

    def _get_auth_headers(self) -> Dict[str, str]:
        headers = {
            "accept": "*/*",
            "accept-language": "en-US,en;q=0.9",
            "cache-control": "no-cache",
            "content-type": "application/json",
            "dnt": "1",
            "origin": "https://app.rignite.app",
            "pragma": "no-cache",
            "referer": "https://app.rignite.app/",
            "sec-ch-ua": '"Not-A.Brand";v="99", "Microsoft Edge";v="157", "Chromium";v="157"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-site",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36 Edg/157.0.0.0",
            "x-rignite-ad-policy": "adexium_house:2026-09-06.1",
            "x-tg-platform": "android"
        }
        if self.session_token:
            headers["x-session"] = self.session_token
        return headers

    def sync_taps(self, count: int) -> Dict[str, Any]:
        """Dispatches batch of taps to POST /tap and updates state."""
        headers = self._get_auth_headers()
        payload = {"count": count}

        resp = self.http.post(TAP_ENDPOINT, json=payload, headers=headers, timeout=12)

        # Token expiration auto-handshake recovery (401 Unauthorized)
        if resp.status_code == 401:
            self.log("⚠️ [RIGNITE AUTH] Session token expired (401). Re-acquiring fresh Telegram WebApp session...", "yellow")
            console.print("[Rignite] Session token expired (401). Re-authenticating...")
            self.fetch_fresh_init_data()
            self.authenticate()
            return self.sync_taps(count)

        if resp.status_code == 429:
            retry_after = int(resp.headers.get("x-ratelimit-reset", "20"))
            self.last_status = f"Rate limited. Waiting {retry_after}s..."
            self.log(f"Rate limited (429)! Cooling down for {retry_after}s", "yellow")
            time.sleep(retry_after)
            return self.sync_taps(count)

        resp.raise_for_status()
        data = resp.json()

        # Update authoritative game state
        self.coins = data.get("coins", self.coins)
        self.energy = data.get("energy", self.energy)
        self.battery_energy = data.get("batteryEnergy", self.battery_energy)
        self.battery_cap = data.get("batteryCap", self.battery_cap)
        self.tap_mult = data.get("tapMult", self.tap_mult)
        accepted = data.get("accepted", count)
        self.total_accepted += accepted
        self.session_taps += accepted
        self.batches_sent += 1

        now = time.time()
        if now - self._last_telemetry_emit >= 5.0:
            self._last_telemetry_emit = now
            self.emit_telemetry("tap_sync", {"batch_accepted": accepted})

        return data

    def send_client_log(self, msg: str):
        """Sends genuine telemetry string to POST /client-log."""
        headers = self._get_auth_headers()
        payload = {"msg": msg}
        try:
            resp = self.http.post(CLIENT_LOG_ENDPOINT, json=payload, headers=headers, timeout=10)
            if resp.status_code == 200:
                self.log(f"Telemetry report sent: [dim]{msg[:40]}...[/]", "magenta")
        except Exception as e:
            self.log(f"Telemetry log failed: {e}", "red")

    def create_dashboard(self, current_batch: int = 0, countdown_sec: int = 0) -> Panel:
        """Constructs an updating live dashboard layout using Rich."""
        stats_table = Table(show_header=False, box=None, padding=(0, 2), expand=True)
        stats_table.add_column("Key", style="bold cyan", width=18)
        stats_table.add_column("Val", style="bold white", width=22)
        stats_table.add_column("Key2", style="bold cyan", width=18)
        stats_table.add_column("Val2", style="bold white", width=22)

        user_display = f"@{self.user_info.get('username') or self.user_info.get('first_name', 'Player')}"
        
        # Calculate real-time overall earnings per second (including idle, recharge & waiting times)
        elapsed_sec = max(1.0, time.time() - self.session_start_time)
        coins_earned = max(0, self.coins - (self.start_coins if self.start_coins is not None else self.coins))
        real_cps = coins_earned / elapsed_sec
        real_cpm = real_cps * 60.0

        refill_mode = f"[bold green]{self.full_energy_left} Instant Left[/]" if self.full_energy_left > 0 else "[bold yellow]Natural Recharge (~3u/s)[/]"
        eta_display = f"[bold yellow]{countdown_sec}s remaining[/]" if countdown_sec > 0 else f"[dim]{self.next_action_eta}[/]"

        stats_table.add_row("Bot Target:", f"@{self.bot_username}", "Player User:", user_display)
        stats_table.add_row("Current Coins:", f"[bold yellow]{self.coins:,}[/]", "Energy Remaining:", f"[bold green]{self.energy}[/] / {self.max_energy}")
        stats_table.add_row("Net Tap Delta:", f"[bold yellow]+{coins_earned:,}[/]", "Colony Passive Coins:", f"[bold cyan]{self.colony_coins_total:,}[/]")
        stats_table.add_row("Tap Multiplier:", f"[bold magenta]x{self.tap_mult}[/]", "Total Taps Synced:", f"[bold green]{self.total_accepted:,}[/]")
        stats_table.add_row("Real Rate (CPS):", f"[bold green]{real_cps:.2f} coins/s[/]", "Real Rate (CPM):", f"[green]{real_cpm:,.0f} coins/m[/]")
        stats_table.add_row("Refill Mode:", refill_mode, "Wait / Next Action:", eta_display)
        stats_table.add_row("Battery Energy:", f"[cyan]{self.battery_energy:,}[/] / {self.battery_cap:,}", "Bot Target:", f"@{self.bot_username}")

        # Progress bar of current Energy capacity
        energy_pct = max(0.0, min(100.0, (self.energy / max(1, self.max_energy)) * 100.0))
        filled = int(energy_pct / 3)
        energy_bar = f"[bold green]{'━' * filled}[/][dim white]{'─' * (33 - filled)}[/] {energy_pct:5.1f}%"

        # Recent events
        log_lines = []
        for ts, txt, sty in self.recent_logs:
            log_lines.append(f"[dim]{ts}[/] [{sty}]• {txt}[/{sty}]")
        logs_text = "\n".join(log_lines) if log_lines else "[dim]Engine running smoothly...[/]"

        status_line = f" [bold yellow]{self.last_status}[/bold yellow]"
        if countdown_sec > 0:
            status_line += f" [bold cyan]({countdown_sec}s countdown)[/bold cyan]"

        elapsed_min = int(elapsed_sec // 60)
        elapsed_rem_sec = int(elapsed_sec % 60)
        runtime_str = f"{elapsed_min}m {elapsed_rem_sec}s"

        full_content = (
            stats_table,
            Text.from_markup(f"\n[bold]Current Tank Energy:[/bold] ({self.energy:,} / {self.max_energy:,} points)\n{energy_bar}\n"),
            Panel(Text.from_markup(f"{status_line}\n\n{logs_text}"), title="[bold]Engine Status & Live Logs[/bold]", border_style="dim blue")
        )

        return Panel(
            full_content[0],
            title=f"[bold blue]⚡ Rignite Auto-Pilot Tap Engine ⚡[/bold blue] - [cyan]@{self.bot_username}[/cyan]",
            subtitle=f"[dim]Cycle: #{self.cycle_count} | Session Runtime: {runtime_str} | Batches Synced: {self.batches_sent:,} | Total Taps: {self.total_accepted:,}[/dim]",
            border_style="bright_blue"
        )

    def run_infinite_auto_pilot(self):
        """
        Runs an autonomous, infinite tapping loop:
        1. Empties energy tank with natural human alternations.
        2. Once low, triggers instant daily recharge if available.
        3. If boosts are depleted, calculates optimal recharge time and rests.
        4. Re-syncs profile and resumes tapping.
        """
        self.log(f"Autonomous Auto-Pilot activated. Target: [bold yellow]@{self.bot_username}[/]", "bold green")
        console.print(f"[bold green]▶ Rignite Auto-Pilot Started for @{self.bot_username} (Player: @{self.user_info.get('username') or self.user_info.get('first_name', 'Player')})[/bold green]")
        console.print(f"💰 Initial Balance: {self.coins:,} | Energy: {self.energy}/{self.max_energy} | Refills: {self.full_energy_left}")

        # If stdout is a real terminal, use rich Live HUD; if running inside a supervisor pipe, use clean log streaming
        is_real_tty = sys.stdout.isatty()
        live_ctx = Live(console=console, refresh_per_second=6, transient=False) if is_real_tty else None

        def update_hud(countdown: int = 0):
            if live_ctx:
                live_ctx.update(self.create_dashboard(countdown_sec=countdown))

        last_session_refresh_time = time.time()
        # Refresh Telegram session every 40 minutes for uninterrupted infinite operation
        SESSION_MAX_AGE_SEC = 40 * 60

        try:
            if live_ctx:
                live_ctx.start()

            while True:
                # Proactive Session Cycling Watchdog
                if time.time() - last_session_refresh_time > SESSION_MAX_AGE_SEC:
                    console.print("[Rignite Lifecycle] 40-minute session lifecycle reached. Proactively refreshing Telegram session...")
                    self.fetch_fresh_init_data()
                    self.authenticate()
                    last_session_refresh_time = time.time()

                # Check for manual dashboard commands (e.g. watch ad & refill energy)
                self.check_manual_commands()

                # -------------------------------------------------------------
                # 0. Smart Rewarded Ad Energy Top-Up (Probability 75% when energy < 50%)
                # Ad Burst Logic: watches 2 to 5 ads with 60s cooldown in between,
                # continuing normal tapping during the cooldown gap!
                # -------------------------------------------------------------
                energy_pct = (self.energy / max(1, self.max_energy)) * 100.0
                time_since_ad = time.time() - self.last_ad_energy_time

                # Reset burst state if cooldown between bursts (> 15 minutes) or tank replenished
                if self.burst_count >= self.burst_target and self.burst_target > 0:
                    if time_since_ad > 300:
                        self.burst_count = 0
                        self.burst_target = 0

                # Trigger ad watch condition:
                # 1) Mid-burst: burst active and 60s gap has elapsed
                # 2) New burst: energy < 50%, cooldown >= 60s, and 75% random probability roll
                is_mid_burst = (self.burst_target > 0 and self.burst_count < self.burst_target and time_since_ad >= 60.0)
                should_start_burst = (
                    self.burst_target == 0 and
                    self.energy > 15 and
                    energy_pct < 50.0 and
                    time_since_ad >= 60.0 and
                    self.ad_energy_left > 0 and
                    random.random() < 0.75
                )

                if is_mid_burst or should_start_burst:
                    if should_start_burst:
                        # Determine burst session: watch 2 to 5 ads (bounded by available adEnergyLeft)
                        available = self.ad_energy_left if self.ad_energy_left > 0 else 5
                        self.burst_target = min(available, random.randint(2, 5))
                        self.burst_count = 0
                        self.log(f"⚡ Energy at {energy_pct:.1f}% (<50%). 75% probability triggered: starting {self.burst_target}-ad refill series!", "cyan")

                    current_num = self.burst_count + 1
                    self.log(f"🎬 Watching Ad {current_num}/{self.burst_target} for energy refill...", "cyan")
                    self.last_status = f"Watching ad {current_num}/{self.burst_target} to refill energy..."
                    self.next_action_eta = "Watching rewarded ad..."
                    self.emit_telemetry("ad_prep", {"action_status": self.last_status, "next_action": self.next_action_eta})
                    update_hud()

                    if self.simulate_rewarded_ad("full_energy"):
                        self.burst_count += 1
                        update_hud()
                        if self.burst_count < self.burst_target:
                            self.log(f"✅ Ad {self.burst_count}/{self.burst_target} completed! 60s gap started (you may tap while waiting)...", "green")
                        else:
                            self.log(f"🎉 Ad series completed ({self.burst_count}/{self.burst_target} ads watched)! Energy: {self.energy}/{self.max_energy}", "bold green")
                            self.burst_count = 0
                            self.burst_target = 0
                        time.sleep(1.0)
                        # Re-check manual commands before tapping
                        self.check_manual_commands()
                        continue
                    else:
                        # Ad failed or on server cooldown
                        self.burst_count = 0
                        self.burst_target = 0

                # -------------------------------------------------------------
                # 1. Energy Depletion & Natural Rest Handling
                # -------------------------------------------------------------
                if self.energy <= 15:
                    if self.full_energy_left > 0:
                        if random.random() < 0.80:
                            self.last_status = "Using instant Daily Energy Refill..."
                            self.next_action_eta = "Claiming daily refill..."
                            self.emit_telemetry("boost_prep", {"action_status": self.last_status, "next_action": self.next_action_eta})
                            update_hud()
                            time.sleep(random.uniform(2.5, 5.0))
                            if self.trigger_full_energy_boost():
                                update_hud()
                                time.sleep(1.0)
                                continue
                        else:
                            brief_pause = random.randint(30, 65)
                            self.log(f"Natural human pause ({brief_pause}s) before claiming instant refill...", "dim yellow")
                            for remaining in range(brief_pause, 0, -1):
                                self.check_manual_commands()
                                self.last_status = f"Brief pause before refill ({remaining}s)..."
                                self.next_action_eta = f"Refill in {remaining}s"
                                update_hud(remaining)
                                if remaining % 4 == 0 or remaining <= 2:
                                    self.emit_telemetry("boost_pause", {"action_status": self.last_status, "next_action": self.next_action_eta})
                                time.sleep(1.0)
                            if self.trigger_full_energy_boost():
                                update_hud()
                                continue

                    # B. Rewarded Ad Energy Refills (fallback when depleted)
                    if self.ad_energy_left > 0 and (time.time() - self.last_ad_energy_time >= 60.0):
                        self.last_status = "Refueling energy via rewarded ad..."
                        self.next_action_eta = "Watching rewarded ad..."
                        self.emit_telemetry("ad_prep", {"action_status": self.last_status, "next_action": self.next_action_eta})
                        update_hud()
                        if self.simulate_rewarded_ad("full_energy"):
                            update_hud()
                            time.sleep(1.0)
                            continue

                    # C. Natural Recharge
                    wait_seconds = random.randint(60, 150)
                    self.next_action_eta = f"Natural recharge: {wait_seconds}s"
                    self.log(f"Energy depleted. Resting {wait_seconds}s for battery & energy recharge...", "yellow")
                    console.print(f"🔋 [Rignite Recharge] Resting {wait_seconds}s for battery & energy recharge...")
                    self.emit_telemetry("recharge_start", {"action_status": f"Recharging energy ({wait_seconds}s)", "next_action": f"Recharge ends in {wait_seconds}s"})

                    for remaining in range(wait_seconds, 0, -1):
                        self.check_manual_commands()
                        self.last_status = f"Resting & recharging energy ({remaining}s)..."
                        self.next_action_eta = f"Recharge ends in {remaining}s"
                        update_hud(remaining)
                        if remaining % 4 == 0 or remaining <= 2:
                            self.emit_telemetry("recharging", {"action_status": self.last_status, "next_action": self.next_action_eta})
                        time.sleep(1.0)

                    self.last_status = "Syncing profile after recharge..."
                    self.next_action_eta = "Syncing with server..."
                    update_hud()
                    self.refresh_me()
                    self.cycle_count += 1
                    self.log(f"Recharged! Energy: {self.energy}/{self.max_energy}. Starting cycle #{self.cycle_count}", "green")
                    console.print(f"✅ [Rignite Cycle #{self.cycle_count}] Recharged! Energy: {self.energy}/{self.max_energy} | Coins: {self.coins:,}")
                    self.emit_telemetry("recharge_cycle_complete")
                    continue

                # -------------------------------------------------------------
                # 2. Tap Batch Simulation (14 - 24 taps per HTTP request)
                # -------------------------------------------------------------
                max_batch = min(self.energy, random.randint(14, 24))
                if max_batch <= 0:
                    time.sleep(2)
                    continue

                self.next_action_eta = f"Tapping batch of {max_batch}..."

                for _ in range(max_batch):
                    self.telemetry.record_tap()
                    tap_delay = max(0.10, min(0.24, random.gauss(0.165, 0.028)))
                    self.last_status = f"Fast-tapping (Pole: {self.telemetry.last_pole})..."
                    update_hud()
                    time.sleep(tap_delay)

                # -------------------------------------------------------------
                # 3. Synchronize Batch to /tap
                # -------------------------------------------------------------
                self.last_status = f"Dispatching batch of {max_batch} taps to /tap..."
                update_hud()
                try:
                    result = self.sync_taps(max_batch)
                    accepted = result.get("accepted", max_batch)
                    self.log(f"Synced [bold green]{accepted}[/] taps | Coins: [yellow]{self.coins:,}[/] | Energy: [green]{self.energy}[/]", "white")
                    console.print(f"⚡ [Rignite Tap] Synced {accepted} taps | Coins: {self.coins:,} | Energy: {self.energy}/{self.max_energy}")
                except requests.exceptions.RequestException as e:
                    self.log(f"Tap sync error: {e}", "red")
                    console.print(f"❌ [Rignite Error] Tap sync error: {e}")
                    time.sleep(3)

                # -------------------------------------------------------------
                # 4. Check 500-Tap Behavioral Telemetry Threshold
                # -------------------------------------------------------------
                if self.telemetry.should_report():
                    fp_payload = self.telemetry.build_report_payload()
                    if fp_payload:
                        self.send_client_log(fp_payload)

                # -------------------------------------------------------------
                # 5. Natural Inter-Batch Pause (0.25s - 0.65s)
                # -------------------------------------------------------------
                pause_time = random.uniform(0.25, 0.65)
                self.last_status = f"Micro-pause ({pause_time:.1f}s)..."
                update_hud()
                time.sleep(pause_time)
                self.check_manual_commands()

                # -------------------------------------------------------------
                # 6. Natural Periodic Colony Passive Income & Rewards Collection
                # Occurs naturally at calm, organic intervals with good gaps
                # Separated from tap velocity and net coins delta!
                # Respects BATTERY_FULL & ENERJI_DOLU limits.
                # -------------------------------------------------------------
                now_t = time.time()
                # A. Colony Income Collection (Every 10 to 30 mins)
                if (now_t - self.last_colony_collect_time) >= self.next_colony_collect_interval:
                    self.last_colony_collect_time = now_t
                    self.next_colony_collect_interval = float(random.randint(600, 1800))
                    self.last_status = "Collecting colony passive income..."
                    update_hud()
                    self.collect_colony_income()
                    update_hud()

                # B. Power Plant (Santral) generation (Every 15 to 35 mins)
                if (now_t - self.last_santral_collect_time) >= 1200:
                    self.last_santral_collect_time = now_t + random.randint(-180, 420)
                    self.collect_power_plant(no=1)
                    update_hud()

                # C. Daily Streak Reward Check (Every 4 to 8 hours)
                if (now_t - self.last_daily_claim_time) >= 14400:
                    self.last_daily_claim_time = now_t + random.randint(0, 3600)
                    self.claim_daily_reward()
                    update_hud()

                # D. Surprise Box Check (Every 30 to 60 mins)
                if (now_t - self.last_surprise_claim_time) >= 1800:
                    self.last_surprise_claim_time = now_t + random.randint(0, 900)
                    self.claim_surprise_box()
                    update_hud()

                # E. Friend Referrals Commission (Every 1 to 2 hours)
                if (now_t - self.last_friends_claim_time) >= 3600:
                    self.last_friends_claim_time = now_t + random.randint(0, 1800)
                    self.claim_friends_commission()
                    update_hud()

        finally:
            if live_ctx:
                live_ctx.stop()


# ==============================================================================
# Execution Entry Point
# ==============================================================================
if __name__ == "__main__":
    console.clear()
    console.print(Panel.fit(
        "[bold cyan]⚡ Rignite Auto-Pilot Infinite Tap Engine ⚡[/bold cyan]\n"
        "[dim]Bot Target: [bold yellow]@RigniteBot[/bold yellow] | Service: https://api.rignite.app[/dim]\n"
        "[green]Mode 1: Automated SessionMaker Auth -> Infinite Loop with Smart Recharging[/green]",
        border_style="cyan"
    ))

    client = RigniteClient(bot_username=DEFAULT_BOT)

    try:
        # Step 1: Obtain fresh dynamic Telegram WebApp session
        client.fetch_fresh_init_data()

        # Step 2: Authenticate and issue x-session token
        client.authenticate()

        # Step 3: Run fully automated infinite tapping lifecycle
        client.run_infinite_auto_pilot()

    except KeyboardInterrupt:
        console.print("\n[bold yellow]Auto-Pilot paused by user. Cleanly exiting...[/bold yellow]")
    except Exception as err:
        console.print(f"\n[bold red]Fatal error during execution:[/bold red] {err}")

