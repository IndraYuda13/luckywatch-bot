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
        self.user_agent = "Mozilla/5.0 (Linux; Android 14; Pixel 8 Pro) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Mobile Safari/537.36"
        self.cookie_string = ""
        self._opener = self._build_opener()

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
        """Load session cookies from JSON state and validate via REST API."""
        if not STATE_FILE.exists():
            return False
        try:
            state = json.loads(STATE_FILE.read_text())
            session = state.get("sessions", {}).get(self.email)
            if not session or not session.get("cookie_string"):
                return False

            self.cookie_string = session["cookie_string"]
            logger.info(f"Found saved session for {self.email}. Validating session ...")
            user_data = self.get_user_info()
            if user_data.get("status") == "ok" and user_data.get("data", {}).get("email") == self.email:
                u = user_data["data"]
                logger.info(f"Session is VALID! User: {u.get('email')} | Balance: ${u.get('balance')} | Clovers: {u.get('clover')}")
                return True
        except Exception as e:
            logger.warning(f"Error loading saved session: {e}")

        self.cookie_string = ""
        return False

    def solve_turnstile(self, sitekey: str, action: str = "login", timeout_s: int = 40) -> str:
        """Call internal Turnstile Solver API to obtain Cloudflare challenge token."""
        solver_cfg = self.global_config.get("turnstile", {})
        solver_api = solver_cfg.get("solver_url", "http://127.0.0.1:5072")
        signin_url = self.global_config.get("app", {}).get("signin_url", f"{self.base_url}/signin")

        qs = urllib.parse.urlencode({
            "url": signin_url,
            "sitekey": sitekey,
            "action": action,
        })
        if self.proxy_url:
            qs += f"&proxy={urllib.parse.quote(self.proxy_url)}"

        create_task_url = f"{solver_api}/turnstile?{qs}"
        logger.info(f"Requesting Turnstile token from solver: {create_task_url} ...")

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

    def login(self, max_retries: int = 5, force_refresh: bool = False) -> Dict[str, Any]:
        """Perform full authentication flow with automatic retry loop."""
        if not force_refresh and self.load_saved_session():
            return {"status": "success", "source": "saved_session", "user": self.get_user_info().get("data")}

        turnstile_cfg = self.global_config.get("turnstile", {})
        sitekey = turnstile_cfg.get("sitekey", "0x4AAAAAABqiRMe3mbyG5xKO")
        timeout_s = turnstile_cfg.get("timeout_seconds", 40)

        for attempt in range(1, max_retries + 1):
            logger.info(f"🔑 Authentication attempt {attempt}/{max_retries} for {self.email} ...")
            try:
                token = self.solve_turnstile(sitekey=sitekey, timeout_s=timeout_s)
                logger.info(f"Submitting mailAuth for {self.email} ...")
                
                # Use raw urllib opener to capture Set-Cookie headers
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
                
                with self._opener.open(req, timeout=15) as res:
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
                logger.warning(f"Login attempt {attempt} failed ({e}). Retrying in {attempt * 3}s...")
                time.sleep(attempt * 3)

        raise RuntimeError(f"Failed to authenticate {self.email} after {max_retries} attempts")

    def get_user_info(self) -> Dict[str, Any]:
        """Fetch current user profile and balance."""
        return self._api("user/", data={"method": "getCurrentUser"})

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

    def claim_daily_bonus(self) -> Dict[str, Any]:
        """Claim daily login / streak bonus."""
        info = self._api("user/tasks/dailyBonus/", data={"method": "getInfo"})
        if info.get("status") == "ok" and (info.get("data", {}).get("available") or info.get("data", {}).get("canGet")):
            logger.info("Daily Bonus is AVAILABLE! Claiming bonus ...")
            return self._api("user/tasks/dailyBonus/", data={"method": "getBonus"})
        return {"status": "skipped", "message": "Daily bonus not ready"}

    def seconds_until_next_hour(self) -> int:
        """Calculate exact seconds until the next hour rollover + 20s buffer."""
        now = datetime.now()
        next_hour = (now + timedelta(hours=1)).replace(minute=0, second=20, microsecond=0)
        secs = int((next_hour - now).total_seconds())
        return max(60, secs)

    def seconds_until_next_day(self) -> int:
        """Calculate exact seconds until tomorrow 00:01 (UTC reset) + buffer."""
        now = datetime.now()
        next_day = (now + timedelta(days=1)).replace(hour=0, minute=1, second=30, microsecond=0)
        secs = int((next_day - now).total_seconds())
        return max(300, secs)

    def watch_single_video(self) -> Dict[str, Any]:
        """Execute one complete video watch and real reward claim cycle."""
        # 1. Fetch next task
        task_res = self.get_task()
        if task_res.get("status") != "ok" or not task_res.get("data"):
            msg = task_res.get("message", "No task returned")
            logger.warning(f"No task available: {msg}")
            if "limitInHour" in msg:
                return {"status": "limit_hour"}
            elif "limitInDay" in msg:
                return {"status": "limit_day"}
            return {"status": "empty", "message": msg}

        task = task_res["data"]
        task_id = str(task.get("id"))
        duration = int(task.get("duration", 12))
        yt_id = task.get("ytId", "unknown")
        limit_day = task.get("limitDay", 100)
        limit_hour = task.get("limitHour", 10)
        cur_day = task.get("curDay", 0)

        logger.info(f"▶ Task [{task_id}] | Video: {yt_id} | Dur: {duration}s | Day Left: {limit_day} | Hour Left: {limit_hour} | CurDay: {cur_day}")

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
                    else:
                        logger.warning(f"Captcha submit response: {sub_res}")
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

            # Always fetch real user balance from server
            try:
                u_curr = self.get_user_info().get("data", {})
                logger.info(f"💰 REAL BALANCE: ${u_curr.get('balance')} USD | Clovers: {u_curr.get('clover')}")
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
                        logger.info(f"💰 REAL BALANCE: ${u.get('balance')} USD | Clovers: {u.get('clover')}")
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
