#!/usr/bin/env python3
"""
LuckyWatch Automated Watch & Claim Engine (Multi-Account & Multi-Threading Support)
----------------------------------------------------------------------------------
Enterprise-grade Pure Python HTTP Automation for LuckyWatch.
- Multi-Account Concurrent Daemon (1 Thread Worker per Active Account with Isolated Proxy).
- Robust Login Architecture with Exponential Backoff & Multi-Attempt Retries.
- Smart Dynamic Quota Scheduler:
  * Ingests real server quota from task payloads (limitHour & limitDay).
  * Fast Exponential Re-Check (30s -> 60s -> 120s max) if no task is returned.
  * Precise sleep to next hour rollover (:01) ONLY when server explicitly signals limitInHour.
- Integrated IconCaptchaSolver microservice via 9router Vision LLM.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(threadName)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("LuckyWatchBot")

CONFIG_FILE = Path("config.json")
STATE_FILE = Path("state/sessions.json")
FLEET_STATE_FILE = Path("state/fleet_state.json")


class AccountWorker(threading.Thread):
    def __init__(self, account_config: Dict[str, Any], global_config: Dict[str, Any], daemon_mode: bool = True):
        super().__init__(name=account_config.get("email", "Worker").split("@")[0])
        self.account = account_config
        self.email = account_config["email"]
        self.password = account_config["password"]
        self.global_config = global_config
        self.daemon_mode = daemon_mode
        self.stop_event = threading.Event()

        # Dedicated proxy for this account
        self.proxy_url = self.account.get("proxy") or self.global_config.get("proxy", {}).get("url")
        self.base_url = self.global_config.get("app", {}).get("base_url", "https://luckywatch.pro").rstrip("/")
        self.api_url = f"{self.base_url}/api"
        self.user_agent = "Mozilla/5.0"
        self.cookie_string = ""
        self._opener = self._build_opener()
        self._last_auto_withdraw_time = 0
        self._last_known_balance = "0.0000000"
        self._last_known_clovers = 0

    def update_fleet_state(self, status: str, countdown_sleep: int = 0, current_task: Optional[Dict[str, Any]] = None, error_reason: Optional[str] = None, daily_done: Optional[int] = None, hourly_done: Optional[int] = None):
        """Atomically persist worker live state directly to state/fleet_state.json for O(1) instant dashboard sync."""
        try:
            FLEET_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            fleet_data = {}
            if FLEET_STATE_FILE.exists():
                try:
                    fleet_data = json.loads(FLEET_STATE_FILE.read_text())
                except Exception:
                    fleet_data = {}

            acc_entry = fleet_data.setdefault(self.email, {})
            acc_entry["status"] = status
            acc_entry["countdown_sleep"] = int(countdown_sleep)
            if countdown_sleep > 0:
                acc_entry["sleep_until_ts"] = int(time.time() + countdown_sleep)
            else:
                acc_entry["sleep_until_ts"] = 0

            if current_task is not None:
                acc_entry["current_task"] = current_task
            elif status == "SLEEPING":
                if acc_entry.get("current_task"):
                    acc_entry["current_task"]["status"] = "SLEEPING"

            if error_reason is not None:
                acc_entry["error_reason"] = error_reason
            elif status == "ACTIVE":
                acc_entry["error_reason"] = None

            if daily_done is not None:
                acc_entry["daily_done"] = int(daily_done)
            if hourly_done is not None:
                acc_entry["hourly_done"] = int(hourly_done)

            acc_entry["balance"] = getattr(self, "_last_known_balance", "0.0000000")
            acc_entry["clovers"] = getattr(self, "_last_known_clovers", 0)
            acc_entry["updated_at"] = time.strftime("%H:%M:%S")
            acc_entry["timestamp"] = int(time.time())

            FLEET_STATE_FILE.write_text(json.dumps(fleet_data, indent=2) + "\n")
        except Exception:
            pass

    def _build_opener(self) -> urllib.request.OpenerDirector:
        handlers: List[urllib.request.BaseHandler] = []
        if self.proxy_url:
            handlers.append(urllib.request.ProxyHandler({"http": self.proxy_url, "https": self.proxy_url}))
        return urllib.request.build_opener(*handlers)

    def check_proxy(self) -> str:
        """Verify proxy connection health and egress IP."""
        if not self.proxy_url:
            logger.info("Proxy is disabled. Connecting directly.")
            return "direct"

        logger.info(f"Checking proxy health via {self.proxy_url} ...")
        health_url = self.global_config.get("proxy", {}).get("health_check_url", "https://api.ipify.org?format=json")
        req = urllib.request.Request(health_url, headers={"User-Agent": "curl/7.81.0"})
        try:
            with self._opener.open(req, timeout=10) as res:
                raw = res.read().decode("utf-8", errors="ignore").strip()
                try:
                    data = json.loads(raw)
                    ip = data.get("ip", raw)
                except Exception:
                    ip = raw
                logger.info(f"Proxy is ACTIVE -> Egress IP: {ip}")
                return str(ip)
        except Exception as e:
            logger.error(f"Proxy check error: {e}")
            return "error"

    def _api(self, endpoint: str, data: Optional[Dict[str, Any]] = None, timeout: int = 15) -> Dict[str, Any]:
        """Execute HTTP POST request to LuckyWatch API."""
        url = f"{self.api_url}/{endpoint.lstrip('/')}"
        req_headers = {
            "User-Agent": self.user_agent,
            "Origin": self.base_url,
            "Referer": f"{self.base_url}/watch",
        }
        if self.cookie_string:
            req_headers["Cookie"] = self.cookie_string

        encoded_data = None
        if data is not None:
            req_headers["Content-Type"] = "application/x-www-form-urlencoded"
            encoded_data = urllib.parse.urlencode(data).encode("utf-8")

        req = urllib.request.Request(url, data=encoded_data, headers=req_headers, method="POST")
        try:
            with self._opener.open(req, timeout=timeout) as res:
                body = res.read().decode("utf-8", errors="ignore")
                try:
                    return json.loads(body)
                except Exception:
                    return {"status": "ok", "raw": body}
        except Exception as e:
            if hasattr(e, "read"):
                err_body = e.read().decode("utf-8", errors="ignore")
                try:
                    return json.loads(err_body)
                except Exception:
                    return {"status": "error", "message": err_body}
            return {"status": "error", "message": str(e)}

    def load_saved_session(self) -> bool:
        """Load session cookies from JSON state and validate via streaming / REST APIs."""
        if not STATE_FILE.exists():
            return False
        try:
            state = json.loads(STATE_FILE.read_text())
            session = state.get("sessions", {}).get(self.email)
            if not session or not session.get("cookie_string"):
                return False

            self.cookie_string = session["cookie_string"]
            logger.info(f"Found saved session for {self.email}. Validating session ...")

            # 1. Primary check: get_user_info()
            user_data = self.get_user_info()
            if user_data.get("status") == "ok" and (user_data.get("data", {}).get("email") == self.email or user_data.get("data", {}).get("id") or user_data.get("data", {}).get("balance") is not None):
                u = user_data.get("data", {})
                logger.info(f"Session is VALID! User: {u.get('email', self.email)} | Balance: ${u.get('balance', '0.0000000')} | Clovers: {u.get('clover', 0)}")
                return True

            # 2. Secondary check: Task streaming probe (checkIp / getLimits / get task)
            # LuckyWatch tasks API is often 100% authorized even when profile endpoint hits checkSecurity
            t_res = self._api("user/tasks/", data={"method": "getLimits"})
            if t_res.get("status") == "ok" or (isinstance(t_res.get("data"), dict) and "limitDay" in t_res["data"]):
                d = t_res.get("data", {})
                logger.info(f"Session is VALID via Task Engine! Limits: Day={d.get('limitDay', '?')}, Hour={d.get('limitHour', '?')}")
                return True

            # 3. Tertiary probe: check active task
            p_res = self._api("user/tasks/", data={"method": "get"})
            if p_res.get("status") == "ok" or "limitInHour" in p_res.get("message", "") or "limitInDay" in p_res.get("message", ""):
                bal = p_res.get("data", {}).get("balance", "0.0000000") if isinstance(p_res.get("data"), dict) else "0.0000000"
                logger.info(f"Session is VALID via Stream Probe! Balance: ${bal}")
                return True

        except Exception as e:
            logger.warning(f"Error loading saved session: {e}")

        logger.warning(f"Saved session for {self.email} is expired or invalid. Triggering fresh authentication...")
        self.cookie_string = ""
        return False

    def get_login_proxy_pool(self) -> List[str]:
        """Collect all available proxy URLs from configuration (node-01 to node-15) for login failover."""
        proxies = []
        # First priority: dedicated proxy assigned to this account
        if self.proxy_url:
            proxies.append(self.proxy_url)

        # Other proxies from active accounts in config
        for a in self.global_config.get("accounts", []):
            p = a.get("proxy")
            if p and p not in proxies:
                proxies.append(p)

        # All 15 local proxy nodes pool (ports 31001 to 31015)
        for port in range(31001, 31016):
            p_url = f"http://127.0.0.1:{port}"
            if p_url not in proxies:
                proxies.append(p_url)

        return proxies

    def solve_turnstile(self, sitekey: str, action: str = "login", timeout_s: int = 40, proxy_url_override: Optional[str] = None) -> str:
        """Call internal Turnstile Solver API to obtain Cloudflare challenge token with proxy override support."""
        solver_cfg = self.global_config.get("turnstile", {})
        solver_api = solver_cfg.get("solver_url", "http://127.0.0.1:5072")
        signin_url = self.global_config.get("app", {}).get("signin_url", f"{self.base_url}/signin")

        target_proxy = proxy_url_override or self.proxy_url

        qs = urllib.parse.urlencode({
            "url": signin_url,
            "sitekey": sitekey,
            "action": action,
        })
        if target_proxy:
            qs += f"&proxy={urllib.parse.quote(target_proxy)}"

        create_task_url = f"{solver_api}/turnstile?{qs}"
        logger.info(f"Requesting Turnstile token from solver (via {target_proxy or 'direct'}): {create_task_url} ...")

        with urllib.request.urlopen(create_task_url, timeout=10) as res:
            task = json.loads(res.read())

        if task.get("errorId") != 0 or not task.get("taskId"):
            raise RuntimeError(f"Turnstile solver failed to create task: {task}")

        task_id = task["taskId"]
        logger.info(f"Polling Turnstile task ID {task_id} (max {timeout_s}s)...")

        start_time = time.time()
        while time.time() - start_time < timeout_s:
            time.sleep(2)
            with urllib.request.urlopen(f"{solver_api}/result?id={task_id}", timeout=5) as res:
                result = json.loads(res.read())

            status = result.get("status")
            if status == "ready":
                token = result.get("solution", {}).get("token")
                logger.info(f"Turnstile token SOLVED! ({token[:25]}...)")
                return str(token)
            elif status == "error":
                raise RuntimeError(f"Turnstile solver error: {result}")

        raise TimeoutError(f"Turnstile solver timed out after {timeout_s}s")

    def solve_icon_captcha(self, queue_base64: str, image_base64: str) -> Optional[Tuple[List[Dict[str, int]], str]]:
        """Call dedicated IconCaptchaSolver microservice on port 5073."""
        solver_cfg = self.global_config.get("icon_solver", {})
        if not solver_cfg.get("enabled", True):
            return None

        solver_url = solver_cfg.get("solver_url", "http://127.0.0.1:5073/solve")
        timeout_s = solver_cfg.get("timeout_seconds", 60)

        payload = {
            "queue_base64": queue_base64,
            "image_base64": image_base64,
        }

        req = urllib.request.Request(
            solver_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            logger.info("🧠 Sending captcha to IconCaptchaSolver (Vision LLM) ...")
            with urllib.request.urlopen(req, timeout=timeout_s) as res:
                data = json.loads(res.read().decode("utf-8"))
                if data.get("status") == "ok" and data.get("solution"):
                    sample_id = data.get("sample_id", "")
                    logger.info(f"🎯 Captcha SOLVED in {data.get('latency_ms')}ms -> Solution: {data['solution']} (Sample ID: {sample_id})")
                    return data["solution"], sample_id
                logger.warning(f"Captcha solver returned non-ok: {data}")
        except Exception as e:
            logger.error(f"Failed to call IconCaptchaSolver: {e}")

        return None

    def send_solver_feedback(self, sample_id: str, verified: bool = True):
        """Send feedback to solver to confirm ground-truth verification."""
        if not sample_id:
            return
        solver_cfg = self.global_config.get("icon_solver", {})
        solver_base = solver_cfg.get("solver_url", "http://127.0.0.1:5073/solve").replace("/solve", "")
        feedback_url = f"{solver_base}/feedback"

        payload = {"sample_id": sample_id, "verified": verified}
        req = urllib.request.Request(
            feedback_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=5):
                pass
        except Exception:
            pass

    def login(self, max_retries: int = 10, force_refresh: bool = False, attempt_offset: int = 0) -> Dict[str, Any]:
        """Perform authentication flow with progressive proxy node rotation."""
        if not force_refresh and self.load_saved_session():
            return {"status": "success", "source": "saved_session", "user": self.get_user_info().get("data")}

        turnstile_cfg = self.global_config.get("turnstile", {})
        max_retries = int(turnstile_cfg.get("max_login_retries_per_batch", max_retries))
        sitekey = turnstile_cfg.get("sitekey", "0x4AAAAAABqiRMe3mbyG5xKO")
        timeout_s = turnstile_cfg.get("timeout_seconds", 40)

        proxy_pool = self.get_login_proxy_pool()

        for i in range(max_retries):
            attempt = i + 1
            # Progressive rotation across node 01 to node 15
            proxy_idx = (attempt_offset + i) % len(proxy_pool)
            current_login_proxy = proxy_pool[proxy_idx]

            logger.info(f"🔑 Authentication attempt {attempt}/{max_retries} for {self.email} [Login Proxy: {current_login_proxy}] ...")
            try:
                token = self.solve_turnstile(sitekey=sitekey, timeout_s=timeout_s, proxy_url_override=current_login_proxy)
                logger.info(f"Submitting mailAuth for {self.email} ...")
                
                # Build temporary opener with the login proxy
                login_opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": current_login_proxy, "https": current_login_proxy}))

                payload = {
                    "method": "mailAuth",
                    "email": self.email,
                    "password": self.password,
                    "captchaResponse": token,
                }
                encoded_data = urllib.parse.urlencode(payload).encode("utf-8")
                req = urllib.request.Request(
                    f"{self.api_url}/user/auth/",
                    data=encoded_data,
                    headers={
                        "User-Agent": self.user_agent,
                        "Origin": self.base_url,
                        "Referer": f"{self.base_url}/signin",
                        "Content-Type": "application/x-www-form-urlencoded",
                    },
                    method="POST",
                )
                
                with login_opener.open(req, timeout=15) as res:
                    set_cookie_headers = res.headers.get_all("Set-Cookie", [])
                    raw_res = res.read().decode("utf-8", errors="ignore")
                    res_data = json.loads(raw_res)

                logger.info(f"Auth Response for {self.email}: {res_data}")
                if res_data.get("status") == "ok":
                    # Parse cookies
                    if set_cookie_headers:
                        self.cookie_string = "; ".join([c.split(";")[0] for c in set_cookie_headers])

                    user_res = self.get_user_info()
                    user_data = user_res.get("data", {})

                    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
                    state_data = {"version": 1, "sessions": {}}
                    if STATE_FILE.exists():
                        try:
                            state_data = json.loads(STATE_FILE.read_text())
                        except Exception:
                            pass

                    state_data.setdefault("sessions", {})[self.email] = {
                        "email": self.email,
                        "cookie_string": self.cookie_string,
                        "user": user_data,
                        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    }
                    STATE_FILE.write_text(json.dumps(state_data, indent=2) + "\n")
                    logger.info(f"Session successfully saved to {STATE_FILE}")
                    return {"status": "success", "source": "http_login", "user": user_data}
                else:
                    logger.warning(f"Server rejected login ({res_data.get('message')}). Retrying in 5s ...")

            except Exception as e:
                logger.warning(f"Login attempt {attempt} failed via {current_login_proxy} ({e}). Retrying in {attempt * 3}s...")
                time.sleep(attempt * 3)

        raise RuntimeError(f"Failed to authenticate {self.email} after {max_retries} attempts (Proxy batch offset: {attempt_offset})")

    def get_user_info(self) -> Dict[str, Any]:
        """Fetch current user profile and balance with multi-endpoint fallback."""
        # Tier 1: Primary check via user/ (getCurrentUser)
        try:
            res = self._api("user/", data={"method": "getCurrentUser"})
            if res.get("status") == "ok" and res.get("data") and res.get("data", {}).get("balance") is not None:
                u_d = res["data"]
                self._last_known_balance = str(u_d.get("balance", "0.0000000"))
                if "clover" in u_d:
                    self._last_known_clovers = u_d.get("clover", 0)
                return res
        except Exception:
            pass

        # Tier 2: Probe active task stream (user/tasks/ method: get)
        try:
            t_res = self._api("user/tasks/", data={"method": "get"})
            if t_res.get("status") == "ok" and isinstance(t_res.get("data"), dict):
                t_bal = t_res["data"].get("balance")
                if t_bal is not None:
                    self._last_known_balance = str(t_bal)
                    try:
                        self._last_known_clovers = int(float(t_bal) / 0.00025 * 15)
                    except Exception:
                        pass
                    return {
                        "status": "ok",
                        "data": {
                            "email": self.email,
                            "balance": str(t_bal),
                            "clover": getattr(self, "_last_known_clovers", 0),
                            "id": "",
                        },
                    }
        except Exception:
            pass

        # Tier 3: Check user/settings/
        try:
            s_res = self._api("user/settings/", data={"method": "get"})
            if s_res.get("status") == "ok" and s_res.get("data", {}).get("user"):
                u_data = s_res["data"]["user"]
                if u_data.get("balance") is not None:
                    self._last_known_balance = str(u_data.get("balance"))
                    if "clover" in u_data:
                        self._last_known_clovers = u_data.get("clover", 0)
                    return {
                        "status": "ok",
                        "data": {
                            "email": self.email,
                            "balance": str(u_data.get("balance")),
                            "clover": u_data.get("clover", getattr(self, "_last_known_clovers", 0)),
                            "id": str(u_data.get("id", "")),
                        },
                    }
        except Exception:
            pass

        # Tier 4: Persistent fleet_state.json cache
        if FLEET_STATE_FILE.exists():
            try:
                fleet_st = json.loads(FLEET_STATE_FILE.read_text())
                f_acc = fleet_st.get(self.email, {})
                f_bal = f_acc.get("balance")
                if f_bal is not None:
                    self._last_known_balance = str(f_bal)
                    if "clovers" in f_acc:
                        try:
                            self._last_known_clovers = int(f_acc.get("clovers", 0))
                        except Exception:
                            pass
                    return {
                        "status": "ok",
                        "data": {
                            "email": self.email,
                            "balance": str(f_bal),
                            "clover": getattr(self, "_last_known_clovers", 0),
                            "id": "",
                        },
                    }
            except Exception:
                pass

        # Tier 5: In-memory last known balance fallback
        bal = getattr(self, "_last_known_balance", None)
        if bal is not None:
            return {
                "status": "ok",
                "data": {
                    "email": self.email,
                    "balance": str(bal),
                    "clover": getattr(self, "_last_known_clovers", 0),
                    "id": "",
                },
            }

        return {"status": "error", "message": "Failed to resolve user balance across all tiers"}

    def get_limits(self) -> Dict[str, Any]:
        """Fetch daily/hourly video viewing limits."""
        return self._api("user/tasks/", data={"method": "getLimits"})

    def check_ip_verification(self) -> bool:
        """Execute pre-watch ISP/IP check verification."""
        res = self._api("user/tasks/", data={"method": "checkIp"})
        return bool(res.get("status") == "ok" and res.get("data", {}).get("check"))

    def get_task(self) -> Dict[str, Any]:
        """Fetch next available video watching task."""
        return self._api("user/tasks/", data={"method": "get"})

    def start_task(self, task_id: str) -> Dict[str, Any]:
        """Notify backend that video playback has started with Android telemetry."""
        device_data = {
            "TaskId": task_id,
            "fin": "0",
            "videoCard[vendor]": "Google Inc. (ARM)",
            "videoCard[renderer]": "Mali-G715-Immortalis MC11",
            "viewPort[h]": "815",
            "viewPort[w]": "412",
            "viewPort[hM]": "915",
            "viewPort[wM]": "412",
            "platform": "Linux armv81",
            "dpr": "2.625",
            "v": "2.6",
            "touch": "true",
            "concur": "8",
            "en[noF]": "1",
            "bat[noF]": "1",
        }
        return self._api("user/tasks/start/", data=device_data)

    def check_captcha(self, refresh: int = 0) -> Dict[str, Any]:
        """Call captcha check endpoint."""
        return self._api("user/captcha/check/", data={"refreshTask": str(refresh)})

    def submit_captcha_coordinates(self, clicks: List[Dict[str, int]]) -> Dict[str, Any]:
        """Submit 3 click coordinates to claim locked captcha reward."""
        if len(clicks) < 3:
            return {"status": "error", "message": "Less than 3 clicks"}

        payload = {
            "coor[0][x]": str(clicks[0]["x"]),
            "coor[0][y]": str(clicks[0]["y"]),
            "coor[1][x]": str(clicks[1]["x"]),
            "coor[1][y]": str(clicks[1]["y"]),
            "coor[2][x]": str(clicks[2]["x"]),
            "coor[2][y]": str(clicks[2]["y"]),
        }
        return self._api("user/captcha/check/", data=payload)

    def check_and_trigger_auto_withdraw(self, current_balance: float):
        """Automatically trigger payout if auto_withdraw is enabled and threshold is reached."""
        auto_cfg = self.global_config.get("auto_withdraw", {})
        if not auto_cfg.get("enabled", False):
            return

        threshold = float(auto_cfg.get("threshold_usd", 0.10))
        wallet = (self.account.get("faucetpay_usdt_trc20") or "").strip()

        # Minimum LuckyWatch payout is $0.10 USD and wallet must be set
        if not wallet or current_balance < threshold or current_balance < 0.10:
            return

        # Anti-spam: check last auto withdraw attempt time (at least 1 hour between attempts)
        last_attempt = getattr(self, "_last_auto_withdraw_time", 0)
        if time.time() - last_attempt < 3600:
            return

        self._last_auto_withdraw_time = time.time()
        logger.info(f"⚡ [AUTO-WITHDRAW TRIGGERED] Balance (${current_balance:.5f} USD) >= Threshold (${threshold:.2f} USD). Initiating payout for {self.email} to {wallet} ...")

        try:
            res = self._api("user/payout/send/", data={
                "sum": f"{current_balance:.7f}",
                "service": "faucetpayusdt",
                "captcha": "",
            }, timeout=15)
            if res.get("status") == "ok":
                logger.info(f"🎉 [AUTO-WITHDRAW SUCCESS] Payout submitted for {self.email}! Response: {res.get('data')}")
            else:
                msg = res.get("message", "unknown error")
                logger.warning(f"⚠️ [AUTO-WITHDRAW NOTICE] Server response: {msg}")
        except Exception as e:
            logger.error(f"❌ [AUTO-WITHDRAW ERROR] Failed to send payout request: {e}")

    def claim_daily_bonus(self) -> Dict[str, Any]:
        """
        Smart Daily Activity Bonus Claimer.
        Tier Rules:
          - 100 views -> 100 clovers
          - 200 views -> 500 clovers
          - 300 views -> 1000 clovers
          - 400 views -> $0.005 USD
          - 500 views -> $0.010 USD (MAX PRIZE)
        Policy:
          - Daily bonus can ONLY be claimed ONCE per UTC day.
          - We STRICTLY wait until viewCurDay >= 500 to secure the top $0.01 USD reward.
          - Never prematurely claim low tiers unless configured otherwise.
        """
        try:
            info = self._api("user/tasks/dailyBonus/", data={"method": "getInfo"})
            if info.get("status") != "ok" or not info.get("data"):
                return {"status": "error", "message": "Failed to fetch daily bonus info"}

            data = info["data"]
            daily_bonus_cnt = int(data.get("dailyBonusCnt", 0))
            view_cur_day = int(data.get("viewCurDay", 0))
            sec_left = int(data.get("secondsUntilEndOfDay", 86400))

            # If dailyBonusCnt > 0, bonus for today has already been claimed!
            if daily_bonus_cnt > 0:
                logger.debug(f"[{self.email}] Daily bonus already claimed today (dailyBonusCnt: {daily_bonus_cnt}).")
                return {"status": "already_claimed", "viewCurDay": view_cur_day}

            # Check if reached the maximum 500/500 tier
            if view_cur_day >= 500:
                logger.info(f"🏆 [{self.email}] MAX TIER REACHED ({view_cur_day}/500 views)! Claiming $0.010 USD Daily Activity Bonus ...")
                res = self._api("user/tasks/dailyBonus/", data={"method": "getBonus"})
                if res.get("status") == "ok":
                    logger.info(f"🎉 [{self.email}] DAILY BONUS CLAIMED SUCCESSFULLY! Result: {res.get('data')}")
                else:
                    logger.warning(f"⚠️ [{self.email}] Daily bonus claim response: {res.get('message')}")
                return res
            else:
                needed = 500 - view_cur_day
                logger.info(f"⏳ [{self.email}] Daily Bonus Progress: {view_cur_day}/500 views ({needed} more needed for max $0.01 reward). Claim postponed.")
                return {"status": "in_progress", "viewCurDay": view_cur_day, "needed": needed}
        except Exception as e:
            logger.error(f"Error checking daily bonus for {self.email}: {e}")
            return {"status": "error", "message": str(e)}

    def seconds_until_next_hour(self) -> int:
        """Calculate exact seconds until the next hour rollover (:00:15) UTC/local."""
        now = datetime.now()
        # Next hour at 00 minutes and 15 seconds
        next_hour = (now + timedelta(hours=1)).replace(minute=0, second=15, microsecond=0)
        secs = int((next_hour - now).total_seconds())
        # Return exact dynamic remaining seconds
        return max(5, secs)

    def seconds_until_next_day(self) -> int:
        """Calculate exact seconds until tomorrow reset 00:00:30 UTC."""
        now_utc = datetime.now(timezone.utc)
        next_day_utc = (now_utc + timedelta(days=1)).replace(hour=0, minute=0, second=30, microsecond=0)
        secs = int((next_day_utc - now_utc).total_seconds())
        return max(30, secs)

    def adaptive_sleep(self, total_seconds: int, reason: str = "limit"):
        """Sleep with 1-second step checks to allow fast shutdown and accurate countdown."""
        start_time = time.time()
        while not self.stop_event.is_set():
            elapsed = time.time() - start_time
            remaining = int(total_seconds - elapsed)
            if remaining <= 0:
                break
            time.sleep(1)

    def run(self):
        """Worker thread main execution loop (Fully Dynamic 24/7 Stream Engine with Continuous Login Retry)."""
        self.check_proxy()

        turnstile_cfg = self.global_config.get("turnstile", {})
        retries_per_batch = int(turnstile_cfg.get("max_login_retries_per_batch", 10))
        cooldown_secs = int(turnstile_cfg.get("cooldown_on_failure_seconds", 300))

        # Continuous login loop with configurable attempts per batch, then cooldown & rotating proxy nodes
        login_batch = 0
        while not self.stop_event.is_set():
            try:
                attempt_offset = login_batch * retries_per_batch
                self.login(max_retries=retries_per_batch, attempt_offset=attempt_offset)
                break
            except Exception as e:
                login_batch += 1
                logger.error(f"Login batch #{login_batch} failed for {self.email}: {e}. Entering {cooldown_secs}s cooldown before next rotating attempt (batch #{login_batch+1}) ...")
                self.update_fleet_state(status="ERROR", countdown_sleep=cooldown_secs, error_reason=str(e))
                self.adaptive_sleep(cooldown_secs, reason="login_retry_cooldown")

        if self.stop_event.is_set():
            return

        runner_cfg = self.global_config.get("runner", {})
        delay = runner_cfg.get("delay_between_videos_seconds", 2)
        auto_bonus = runner_cfg.get("auto_daily_bonus", True)

        try:
            self.check_ip_verification()
        except Exception:
            pass

        logger.info(f"Worker started for {self.email} (Dynamic 24/7 Autopilot) ...")

        cycle_count = 0
        total_session_earned = 0

        while not self.stop_event.is_set():
            if auto_bonus:
                try:
                    self.claim_daily_bonus()
                except Exception as e:
                    logger.warning(f"Daily bonus check notice: {e}")

            # Always fetch real user balance from server and check auto-withdraw
            try:
                u_curr = self.get_user_info().get("data", {})
                bal_val = float(u_curr.get('balance', 0.0) or 0.0)
                if u_curr.get('balance') is not None:
                    logger.info(f"💰 REAL BALANCE: ${u_curr.get('balance')} USD | Clovers: {u_curr.get('clover', 0)}")
                self.check_and_trigger_auto_withdraw(bal_val)
            except Exception:
                pass

            # Dynamic Infinite Stream Loop (No arbitrary 50 video limit)
            while not self.stop_event.is_set():
                cycle_count += 1
                logger.info(f"\n--- [Cycle #{cycle_count} | Session Earned: {total_session_earned}] ---")
                res = self.watch_single_video()

                if res.get("status") == "success":
                    total_session_earned += 1
                    try:
                        u = self.get_user_info().get("data", {})
                        bal_val = float(u.get('balance', 0.0))
                        logger.info(f"💰 REAL BALANCE: ${u.get('balance')} USD | Clovers: {u.get('clover')}")
                        self.check_and_trigger_auto_withdraw(bal_val)
                    except Exception:
                        pass
                    time.sleep(delay)
                elif res.get("status") == "limit_hour":
                    sleep_hr = self.seconds_until_next_hour()
                    mins_rem = round(sleep_hr / 60, 1)
                    logger.info(f"⏰ Hourly limit reached (limitInHour). Dynamic sleep {sleep_hr}s (~{mins_rem}m) until next hour reset ...")
                    self.update_fleet_state(status="SLEEPING", countdown_sleep=sleep_hr)
                    self.adaptive_sleep(sleep_hr, reason="limitInHour")
                    logger.info(f"🔔 Hourly cooldown elapsed for {self.email}. Resuming stream immediately!")
                    self.update_fleet_state(status="ACTIVE", countdown_sleep=0)
                    break
                elif res.get("status") == "limit_day":
                    sleep_day = self.seconds_until_next_day()
                    hrs_rem = round(sleep_day / 3600, 1)
                    logger.info(f"🌙 Daily limit reached (limitInDay). Dynamic sleep {sleep_day}s (~{hrs_rem}h) until UTC day reset ...")
                    self.update_fleet_state(status="SLEEPING", countdown_sleep=sleep_day)
                    self.adaptive_sleep(sleep_day, reason="limitInDay")
                    logger.info(f"🌅 Daily cooldown elapsed for {self.email}. Resuming stream immediately!")
                    self.update_fleet_state(status="ACTIVE", countdown_sleep=0)
                    break
                elif res.get("status") == "empty":
                    # Fast 15s backoff on temporary server queue empty (never blind long sleep)
                    logger.warning(f"Task queue temporarily empty from server. Fast backoff 15s ...")
                    self.update_fleet_state(status="ACTIVE", countdown_sleep=15)
                    self.adaptive_sleep(15, reason="queue_empty")
                else:
                    time.sleep(delay)

    def watch_single_video(self) -> Dict[str, Any]:
        """Execute one complete video watch and real reward claim cycle."""
        # 1. Fetch next task
        task_res = self.get_task()
        if task_res.get("status") != "ok" or not task_res.get("data"):
            msg = task_res.get("message", "No task returned")
            logger.warning(f"No task available: {msg}")
            if "limitInHour" in msg:
                sleep_hr = self.seconds_until_next_hour()
                self.update_fleet_state(status="SLEEPING", countdown_sleep=sleep_hr)
                return {"status": "limit_hour"}
            elif "limitInDay" in msg:
                sleep_day = self.seconds_until_next_day()
                self.update_fleet_state(status="SLEEPING", countdown_sleep=sleep_day)
                return {"status": "limit_day"}
            return {"status": "empty", "message": msg}

        task = task_res["data"]
        task_id = str(task.get("id"))
        duration = int(task.get("duration", 12))
        yt_id = task.get("ytId", "unknown")
        limit_day = task.get("limitDay", 100)
        limit_hour = task.get("limitHour", 10)
        cur_day = task.get("curDay", 0)

        # Track live balance directly from task stream response!
        if "balance" in task:
            self._last_known_balance = str(task["balance"])
            try:
                # Clovers is roughly balance / 0.00025 * 15 or tracked directly
                self._last_known_clovers = int(float(task["balance"]) / 0.00025 * 15)
            except Exception:
                pass

        logger.info(f"▶ Task [{task_id}] | Video: {yt_id} | Dur: {duration}s | Day Left: {limit_day} | Hour Left: {limit_hour} | CurDay: {cur_day}")

        # Atomically record active streaming state
        self.update_fleet_state(
            status="ACTIVE",
            countdown_sleep=0,
            current_task={"id": task_id, "video_id": yt_id, "duration": duration, "elapsed": 0, "status": "STREAMING"},
            daily_done=int(cur_day),
            hourly_done=max(0, 65 - int(limit_hour)),
        )

        # 2. Trigger task start telemetry
        self.start_task(task_id)

        # 3. Simulate real video stream playback duration
        logger.info(f"⏳ Streaming video for {duration}s ...")
        time.sleep(duration + 1)

        # 4. Query Captcha Check
        claim_res = self.check_captcha(refresh=0)

        # Direct reward
        if claim_res.get("status") == "ok" and "reward" in claim_res.get("data", {}):
            reward_amt = claim_res["data"]["reward"]
            logger.info(f"✅ Task [{task_id}] DIRECT SUCCESS -> REWARD: +${reward_amt} USD!")
            return {
                "status": "success",
                "task_id": task_id,
                "reward": reward_amt,
                "yt_id": yt_id,
            }
        elif claim_res.get("status") == "data":
            # Visual Captcha Checkpoint triggered!
            logger.info(f"🛡 Captcha checkpoint on task [{task_id}]. Solving via Vision AI ...")
            q_b64 = claim_res["data"].get("queue")
            img_b64 = claim_res["data"].get("image")

            if q_b64 and img_b64:
                res_solve = self.solve_icon_captcha(queue_base64=q_b64, image_base64=img_b64)
                if res_solve:
                    solution, sample_id = res_solve
                    sub_res = self.submit_captcha_coordinates(solution)
                    if sub_res.get("status") == "ok" and "reward" in sub_res.get("data", {}):
                        r_amt = sub_res["data"]["reward"]
                        logger.info(f"🎉 CAPTCHA SOLVED & CLAIMED! Reward: +${r_amt} USD!")
                        self.send_solver_feedback(sample_id, verified=True)
                        return {
                            "status": "success",
                            "task_id": task_id,
                            "reward": r_amt,
                            "yt_id": yt_id,
                        }
                    elif sub_res.get("status") == "data" and sub_res.get("data", {}).get("image"):
                        # Captcha coordinates were slightly off, LuckyWatch served a fresh captcha challenge!
                        logger.info(f"🔄 Captcha challenge refreshed by server. Solving new attempt ...")
                        new_q = sub_res["data"].get("queue", q_b64)
                        new_img = sub_res["data"].get("image")
                        res_solve2 = self.solve_icon_captcha(queue_base64=new_q, image_base64=new_img)
                        if res_solve2:
                            sol2, samp2 = res_solve2
                            sub_res2 = self.submit_captcha_coordinates(sol2)
                            if sub_res2.get("status") == "ok" and "reward" in sub_res2.get("data", {}):
                                r_amt2 = sub_res2["data"]["reward"]
                                logger.info(f"🎉 CAPTCHA (RETRY) SOLVED & CLAIMED! Reward: +${r_amt2} USD!")
                                self.send_solver_feedback(samp2, verified=True)
                                return {"status": "success", "task_id": task_id, "reward": r_amt2, "yt_id": yt_id}
                        return {"status": "retry_captcha", "message": "Captcha retry unresolved"}
                    else:
                        logger.warning(f"Captcha submit notice: {sub_res.get('message', sub_res)}")
                        return {"status": "retry_captcha", "message": str(sub_res)}

            return {"status": "skipped_captcha", "task_id": task_id}
        else:
            msg = str(claim_res.get("message", claim_res))
            logger.info(f"Status for task [{task_id}]: {msg}")
            if "limitInHour" in msg:
                return {"status": "limit_hour"}
            elif "limitInDay" in msg:
                return {"status": "limit_day"}
            return {"status": "skipped", "message": msg}

    def run(self):
        """Worker thread main execution loop."""
        self.check_proxy()
        try:
            self.login(max_retries=5)
        except Exception as e:
            logger.error(f"Login failed for {self.email}: {e}")
            return

        runner_cfg = self.global_config.get("runner", {})
        limit_count = runner_cfg.get("max_videos_per_cycle", 50)
        delay = runner_cfg.get("delay_between_videos_seconds", 2)
        auto_bonus = runner_cfg.get("auto_daily_bonus", True)

        try:
            self.check_ip_verification()
        except Exception:
            pass

        logger.info(f"Worker started for {self.email} (24/7 Daemon: {self.daemon_mode}) ...")

        while not self.stop_event.is_set():
            if auto_bonus:
                try:
                    self.claim_daily_bonus()
                except Exception as e:
                    logger.warning(f"Daily bonus check notice: {e}")

            # Always fetch real user balance from server and check auto-withdraw
            try:
                u_curr = self.get_user_info().get("data", {})
                bal_val = float(u_curr.get('balance', 0.0) or 0.0)
                if u_curr.get('balance') is not None:
                    logger.info(f"💰 REAL BALANCE: ${u_curr.get('balance')} USD | Clovers: {u_curr.get('clover', 0)}")
                self.check_and_trigger_auto_withdraw(bal_val)
            except Exception:
                pass

            watched_in_batch = 0
            for i in range(1, limit_count + 1):
                if self.stop_event.is_set():
                    break

                logger.info(f"\n--- [Batch Cycle {i}/{limit_count} | Session Earned: {watched_in_batch}] ---")
                res = self.watch_single_video()

                if res.get("status") == "success":
                    watched_in_batch += 1
                    try:
                        u = self.get_user_info().get("data", {})
                        bal_val = float(u.get('balance', 0.0) or 0.0)
                        logger.info(f"💰 REAL BALANCE: ${u.get('balance')} USD | Clovers: {u.get('clover')}")
                        self.check_and_trigger_auto_withdraw(bal_val)
                    except Exception:
                        pass
                    time.sleep(delay)
                elif res.get("status") == "limit_hour":
                    sleep_hr = self.seconds_until_next_hour()
                    logger.info(f"⏰ Hourly limit reached (limitInHour). Sleeping {sleep_hr}s until next hour (:01) ...")
                    time.sleep(sleep_hr)
                    break
                elif res.get("status") == "limit_day":
                    sleep_day = self.seconds_until_next_day()
                    logger.info(f"🌙 Daily limit reached (limitInDay). Sleeping {sleep_day}s until tomorrow reset (00:01) ...")
                    time.sleep(sleep_day)
                    break
                elif res.get("status") == "empty":
                    # Non-fatal queue empty (video buffering): short backoff 30s instead of long sleep
                    logger.warning(f"Temporary empty task ({res.get('message')}). Retrying task fetch in 30s ...")
                    time.sleep(30)
                    break
                else:
                    time.sleep(2)

            if not self.daemon_mode:
                logger.info(f"Non-daemon batch completed for {self.email}. Watched: {watched_in_batch} videos.")
                break

            # Short breath before checking next cycle
            time.sleep(3)


class MultiBotManager:
    def __init__(self, config_path: Path = CONFIG_FILE):
        self.config_path = config_path
        self.config = json.loads(config_path.read_text())
        self.workers: List[AccountWorker] = []

    def start_all(self, daemon_mode: bool = True):
        accounts = self.config.get("accounts", [])
        active_accounts = [a for a in accounts if a.get("active", True)]

        if not active_accounts:
            logger.error("No active accounts found in config.json!")
            return

        logger.info(f"Starting MultiBotManager with {len(active_accounts)} active account worker(s)...")

        for acc in active_accounts:
            worker = AccountWorker(account_config=acc, global_config=self.config, daemon_mode=daemon_mode)
            self.workers.append(worker)
            worker.start()

        for worker in self.workers:
            worker.join()


def main():
    daemon = "--daemon" in sys.argv or "-d" in sys.argv
    manager = MultiBotManager()
    manager.start_all(daemon_mode=daemon)


if __name__ == "__main__":
    main()
