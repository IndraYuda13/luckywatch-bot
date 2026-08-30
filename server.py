#!/usr/bin/env python3
"""
LuckyWatch Live Web Dashboard & Telemetry Server (Pure Python Stdlib HTTP Server)
---------------------------------------------------------------------------------
Zero external dependencies (uses standard library http.server + socketserver).
Port: 8280 -> Cloudflare Tunnel: https://luckywatch.indrayuda.my.id

Features:
- Multi-Account Real-Time Telemetry Aggregation (/api/stats)
- Per-Account Detailed Metrics & State Tracking (Proxy, Geo, IP, Status, Task, Balance)
- Filterable Log Streams (Global + Per-Account)
- Action Endpoints (/api/actions/retry, /api/actions/refresh)
- Zero-Blocking In-Memory Cache with Subprocess Management
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn
from typing import Any, Dict, List, Optional, Tuple

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [DashboardServer] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("DashboardServer")

CONFIG_FILE = Path("/root/projects/luckywatch-bot/config.json")
STATE_FILE = Path("/root/projects/luckywatch-bot/state/sessions.json")
FLEET_STATE_FILE = Path("/root/projects/luckywatch-bot/state/fleet_state.json")
LOG_FILE = Path("/root/projects/luckywatch-bot/bot.log")

# In-memory proxy geo cache and user balance cache
_GEO_CACHE: Dict[str, dict] = {}
_LAST_USER_FETCH: Dict[str, dict] = {}
CACHE_TTL_USER = 12  # seconds between upstream balance polling per account


def redact_email(email: str) -> str:
    """Mask email for display / log categorization: 'lv***e@gmail.com'."""
    if "@" not in email:
        return email
    user, domain = email.split("@", 1)
    if len(user) <= 3:
        masked_user = user[0] + "***"
    else:
        masked_user = user[:2] + "***" + user[-1]
    return f"{masked_user}@{domain}"


def get_proxy_geo(proxy_url: str) -> dict:
    """Resolve Egress IP, Country Code, City, and ISP via ip-api with caching."""
    if not proxy_url:
        return {
            "egress_ip": "127.0.0.1",
            "country": "ID",
            "country_name": "Local Direct",
            "city": "Jakarta",
            "isp": "Direct Connection",
        }
    if proxy_url in _GEO_CACHE:
        return _GEO_CACHE[proxy_url]

    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url}))
        req = urllib.request.Request("http://ip-api.com/json/", headers={"User-Agent": "curl/7.81.0"})
        with opener.open(req, timeout=3) as res:
            data = json.loads(res.read().decode("utf-8"))
            geo = {
                "egress_ip": data.get("query", "Unknown"),
                "country": data.get("countryCode", "UN"),
                "country_name": data.get("country", "Unknown"),
                "city": data.get("city", "Unknown"),
                "isp": data.get("isp", "Unknown"),
            }
            _GEO_CACHE[proxy_url] = geo
            return geo
    except Exception as e:
        return {
            "egress_ip": "Proxy Error",
            "country": "ERR",
            "country_name": "Unknown",
            "city": "Unknown",
            "isp": str(e),
        }


def parse_bot_logs(max_lines: int = 150) -> Tuple[List[str], Dict[str, List[str]], Dict[str, Dict[str, Any]]]:
    """Parse tail logs and extract live worker states and per-account log streams."""
    if not LOG_FILE.exists():
        return [], {}, {}

    try:
        lines = LOG_FILE.read_text(errors="ignore").strip().splitlines()
        tail = lines[-max_lines:]
    except Exception:
        tail = []
        lines = []

    account_logs: Dict[str, List[str]] = {}
    account_live_state: Dict[str, Dict[str, Any]] = {}

    # Scan 150 lines only for log stream rendering (state is now independently read from fleet_state.json)
    scan_lines = lines[-200:] if len(lines) > 200 else lines

    for line in scan_lines:
        m = re.match(r"^(\d{2}:\d{2}:\d{2})\s+\[(\w+)\]\s+(?:\[([\w\.-]+)\]\s+)?(.*)$", line)
        if not m:
            continue
        ts, level, tag, msg = m.groups()
        if not tag:
            continue

        tag_clean = tag.lower()
        # Also store into per-account log buffer (up to 150 lines per account)
        account_logs.setdefault(tag_clean, []).append(line)
        if len(account_logs[tag_clean]) > 150:
            account_logs[tag_clean].pop(0)

        state = account_live_state.setdefault(
            tag_clean,
            {
                "status": "ACTIVE",
                "current_task": None,
                "daily_done": 0,
                "daily_cap": 560,
                "hourly_done": 0,
                "hourly_cap": 65,
                "countdown_sleep": 0,
                "error_reason": None,
                "last_activity_time": ts,
            },
        )
        state["last_activity_time"] = ts

        if level == "ERROR":
            state["status"] = "ERROR"
            state["error_reason"] = msg
        elif "▶ Task" in msg:
            t_m = re.search(
                r"Task \[(\d+)\].*?Video:\s*([^\s|]+).*?Dur:\s*(\d+)s.*?Day Left:\s*(\d+).*?Hour Left:\s*(\d+).*?CurDay:\s*(\d+)",
                msg,
            )
            if t_m:
                tid, vid, dur, dl, hl, cd = t_m.groups()
                dur_int = int(dur)
                cd_int = int(cd)
                hl_int = int(hl)
                state["current_task"] = {
                    "id": tid,
                    "video_id": vid,
                    "duration": dur_int,
                    "elapsed": 0,
                    "status": "STREAMING",
                    "started_at": ts,
                }
                state["daily_done"] = cd_int
                state["hourly_done"] = max(0, state["hourly_cap"] - hl_int)
                state["status"] = "ACTIVE"
                state["countdown_sleep"] = 0
                state["error_reason"] = None
        elif "⏳ Streaming" in msg or "⏳ Playing" in msg:
            if state["current_task"]:
                state["current_task"]["status"] = "STREAMING"
            state["status"] = "ACTIVE"
        elif "DIRECT SUCCESS" in msg or "CAPTCHA SOLVED" in msg or "REWARD CLAIMED" in msg:
            if state["current_task"]:
                state["current_task"]["status"] = "CLAIMED"
            state["status"] = "ACTIVE"
        elif "Sleeping" in msg and "s until" in msg:
            s_m = re.search(r"Sleeping (\d+)s", msg)
            if s_m:
                state["countdown_sleep"] = int(s_m.group(1))
                state["status"] = "SLEEPING"
                if state["current_task"]:
                    state["current_task"]["status"] = "SLEEPING"
        elif "Captcha checkpoint" in msg or "Sending captcha" in msg:
            if state["current_task"]:
                state["current_task"]["status"] = "CAPTCHA_SOLVING"
            state["status"] = "ACTIVE"

    return tail, account_logs, account_live_state


def get_latest_stats() -> dict:
    """Aggregate multi-account live stats, fleet summary, and filtered log streams."""
    config_accounts = []
    if CONFIG_FILE.exists():
        try:
            cfg = json.loads(CONFIG_FILE.read_text())
            config_accounts = cfg.get("accounts", [])
        except Exception:
            pass

    sessions_map = {}
    if STATE_FILE.exists():
        try:
            st = json.loads(STATE_FILE.read_text())
            sessions_map = st.get("sessions", {})
        except Exception:
            pass

    # Read deterministic fleet live state directly from state/fleet_state.json
    fleet_states_map = {}
    if FLEET_STATE_FILE.exists():
        try:
            fleet_states_map = json.loads(FLEET_STATE_FILE.read_text())
        except Exception:
            fleet_states_map = {}

    tail_logs, raw_acc_logs, acc_live_states = parse_bot_logs(120)

    now_ts = time.time()
    accounts_list = []
    formatted_acc_logs = {}

    # Optimization: Fetch all account upstream balances in parallel or background thread
    from concurrent.futures import ThreadPoolExecutor

    def fetch_account_upstream(acc_data):
        email, cookie_str, proxy_url = acc_data
        if not cookie_str:
            return email, {}
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url}))
            # Fast profile check
            req = urllib.request.Request(
                "https://luckywatch.pro/api/user/settings/",
                data=urllib.parse.urlencode({"method": "get"}).encode("utf-8"),
                headers={
                    "User-Agent": "Mozilla/5.0",
                    "Cookie": cookie_str,
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
            email_verified = False
            server_wallet_set = False
            with opener.open(req, timeout=3.5) as res:
                u_set = json.loads(res.read().decode("utf-8"))
                if u_set.get("status") == "ok":
                    user_obj = u_set.get("data", {}).get("user", {})
                    services_obj = u_set.get("data", {}).get("services", {})
                    email_verified = user_obj.get("emailactive") == "1"
                    remote_wallet = services_obj.get("faucetpayusdt")
                    server_wallet_set = bool(remote_wallet and remote_wallet.strip())

            # Fast daily bonus check
            req_b = urllib.request.Request(
                "https://luckywatch.pro/api/user/tasks/dailyBonus/",
                data=urllib.parse.urlencode({"method": "getInfo"}).encode("utf-8"),
                headers={
                    "User-Agent": "Mozilla/5.0",
                    "Cookie": cookie_str,
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
            daily_bonus_claimed = False
            daily_bonus_progress = "0/500"
            with opener.open(req_b, timeout=3.5) as res:
                b_set = json.loads(res.read().decode("utf-8"))
                if b_set.get("status") == "ok":
                    b_data = b_set.get("data", {})
                    daily_bonus_cnt = int(b_data.get("dailyBonusCnt", 0))
                    view_cur_day = int(b_data.get("viewCurDay", 0))
                    daily_bonus_claimed = daily_bonus_cnt > 0
                    daily_bonus_progress = f"{view_cur_day}/500"

            # Fast user balance check
            req_u = urllib.request.Request(
                "https://luckywatch.pro/api/user/",
                data=urllib.parse.urlencode({"method": "getCurrentUser"}).encode("utf-8"),
                headers={
                    "User-Agent": "Mozilla/5.0",
                    "Cookie": cookie_str,
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
            bal_val = "0.0000000"
            clov_val = 0
            with opener.open(req_u, timeout=3.5) as res:
                u_data = json.loads(res.read().decode("utf-8"))
                if u_data.get("status") == "ok":
                    bal_val = str(u_data.get("data", {}).get("balance", "0.0000000"))
                    clov_val = int(u_data.get("data", {}).get("clover", 0))

            # Fallback balance probe from user/tasks/ if checkSecurity blocks getCurrentUser
            if bal_val == "0.0000000":
                try:
                    req_t = urllib.request.Request(
                        "https://luckywatch.pro/api/user/tasks/",
                        data=urllib.parse.urlencode({"method": "get"}).encode("utf-8"),
                        headers={"User-Agent": "Mozilla/5.0", "Cookie": cookie_str, "Content-Type": "application/x-www-form-urlencoded"},
                    )
                    with opener.open(req_t, timeout=3.5) as res_t:
                        t_data = json.loads(res_t.read().decode("utf-8"))
                        if t_data.get("status") == "ok" and isinstance(t_data.get("data"), dict):
                            t_bal = t_data["data"].get("balance")
                            if t_bal is not None:
                                bal_val = str(t_bal)
                                clov_val = int(float(t_bal) / 0.00025 * 15)
                except Exception:
                    pass

            # Fetch last payout history
            last_payout = None
            try:
                req_p = urllib.request.Request(
                    "https://luckywatch.pro/api/user/payout/",
                    data=urllib.parse.urlencode({"method": "history", "page": "1"}).encode("utf-8"),
                    headers={
                        "User-Agent": "Mozilla/5.0",
                        "Cookie": cookie_str,
                        "Content-Type": "application/x-www-form-urlencoded",
                    },
                )
                with opener.open(req_p, timeout=3.5) as res:
                    p_res = json.loads(res.read().decode("utf-8"))
                    if p_res.get("status") == "ok":
                        items = p_res.get("data", {}).get("items", [])
                        if items:
                            first = items[0]
                            st_code = str(first.get("status", ""))
                            status_map = {
                                "0": "PAYMENT ERROR",
                                "1": "PAID",
                                "2": "IN PROGRESS",
                                "3": "UNDER REVIEW"
                            }
                            st_label = status_map.get(st_code, f"CODE_{st_code}")
                            ts_val = int(first.get("unixtime", 0))
                            dt_str = datetime.fromtimestamp(ts_val).strftime("%Y-%m-%d %H:%M") if ts_val > 0 else "-"
                            last_payout = {
                                "id": first.get("id"),
                                "amount": str(first.get("val", "0.00000")),
                                "net_amount": str(first.get("commissionVal", "0.00000")),
                                "wallet": first.get("account", ""),
                                "status": st_label,
                                "timestamp": dt_str
                            }
            except Exception:
                pass

            return email, {
                "balance": bal_val,
                "clovers": clov_val,
                "email_verified": email_verified,
                "server_wallet_set": server_wallet_set,
                "daily_bonus_claimed": daily_bonus_claimed,
                "daily_bonus_progress": daily_bonus_progress,
                "last_payout": last_payout,
                "timestamp": time.time(),
            }
        except Exception:
            return email, {}

    # Parallel fetch for all accounts needing fresh data
    accounts_to_fetch = []
    for acc in config_accounts:
        em = acc.get("email", "")
        last_f = _LAST_USER_FETCH.get(em, {})
        if (now_ts - last_f.get("timestamp", 0)) >= CACHE_TTL_USER:
            sess = sessions_map.get(em, {})
            c_str = sess.get("cookie_string", "")
            p_url = acc.get("proxy", "")
            if c_str:
                accounts_to_fetch.append((em, c_str, p_url))

    if accounts_to_fetch:
        with ThreadPoolExecutor(max_workers=5) as executor:
            results = executor.map(fetch_account_upstream, accounts_to_fetch)
            for em, data in results:
                if data:
                    _LAST_USER_FETCH[em] = data

    for acc in config_accounts:
        email = acc.get("email", "")
        redacted = redact_email(email)
        acc_prefix = email.split("@")[0].lower() if "@" in email else email.lower()
        proxy_url = acc.get("proxy", "")
        is_active_cfg = acc.get("active", True)

        geo = get_proxy_geo(proxy_url)
        sess = sessions_map.get(email, {})
        cookie_str = sess.get("cookie_string", "")
        cached_user = sess.get("user", {})

        # Live state from deterministic state file (1st priority) with fallback to log parser
        fleet_st = fleet_states_map.get(email, {})
        log_st = acc_live_states.get(acc_prefix, {})

        # If fleet state explicitly reports SLEEPING / ACTIVE / ERROR, trust fleet_st first
        live_status = fleet_st.get("status") or log_st.get("status") or ("IDLE" if is_active_cfg else "DISABLED")
        live_task = fleet_st.get("current_task") or log_st.get("current_task")
        live_error = fleet_st.get("error_reason") or log_st.get("error_reason")
        live_daily = fleet_st.get("daily_done", log_st.get("daily_done", 0))
        live_hourly = fleet_st.get("hourly_done", log_st.get("hourly_done", 0))
        live_act_time = fleet_st.get("updated_at") or log_st.get("last_activity_time", "-")

        # Calculate exact dynamic sleep countdown from timestamp
        live_sleep_cd = 0
        if fleet_st.get("sleep_until_ts", 0) > now_ts:
            live_sleep_cd = int(fleet_st["sleep_until_ts"] - now_ts)
            live_status = "SLEEPING"
        elif log_st.get("countdown_sleep", 0) > 0:
            live_sleep_cd = int(log_st["countdown_sleep"])

        live_st = {
            "status": live_status,
            "current_task": live_task,
            "daily_done": int(live_daily),
            "daily_cap": 560,
            "hourly_done": int(live_hourly),
            "hourly_cap": 65,
            "countdown_sleep": live_sleep_cd,
            "error_reason": live_error,
            "last_activity_time": live_act_time,
        }

        # User balance resolution & verification status check (cache + fast upstream check)
        balance_val = str(cached_user.get("balance", "0.0000000"))
        clovers_val = int(cached_user.get("clover", 0))
        email_verified = False
        server_wallet_set = False

        last_fetch = _LAST_USER_FETCH.get(email, {})
        daily_bonus_claimed = False
        daily_bonus_progress = "0/500"
        last_payout = None

        if (now_ts - last_fetch.get("timestamp", 0)) < CACHE_TTL_USER:
            balance_val = last_fetch.get("balance", balance_val)
            clovers_val = last_fetch.get("clovers", clovers_val)
            email_verified = last_fetch.get("email_verified", False)
            server_wallet_set = last_fetch.get("server_wallet_set", False)
            daily_bonus_claimed = last_fetch.get("daily_bonus_claimed", False)
            daily_bonus_progress = last_fetch.get("daily_bonus_progress", "0/500")
            last_payout = last_fetch.get("last_payout")
        elif cookie_str:
            try:
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url}))
                req = urllib.request.Request(
                    "https://luckywatch.pro/api/user/settings/",
                    data=urllib.parse.urlencode({"method": "get"}).encode("utf-8"),
                    headers={
                        "User-Agent": "Mozilla/5.0 (Linux; Android 14; Pixel 8 Pro) AppleWebKit/537.36",
                        "Cookie": cookie_str,
                        "Content-Type": "application/x-www-form-urlencoded",
                    },
                )
                with opener.open(req, timeout=3) as res:
                    u_set = json.loads(res.read().decode("utf-8"))
                    if u_set.get("status") == "ok":
                        user_obj = u_set.get("data", {}).get("user", {})
                        services_obj = u_set.get("data", {}).get("services", {})
                        email_verified = user_obj.get("emailactive") == "1"
                        remote_wallet = services_obj.get("faucetpayusdt")
                        server_wallet_set = bool(remote_wallet and remote_wallet.strip())
                        # If user object contains real-time balance
                        if "balance" in user_obj:
                            bal_val = str(user_obj.get("balance", bal_val))
                        if "clover" in user_obj:
                            clov_val = int(user_obj.get("clover", clov_val))

                # Also fetch daily bonus status
                req_b = urllib.request.Request(
                    "https://luckywatch.pro/api/user/tasks/dailyBonus/",
                    data=urllib.parse.urlencode({"method": "getInfo"}).encode("utf-8"),
                    headers={
                        "User-Agent": "Mozilla/5.0 (Linux; Android 14; Pixel 8 Pro) AppleWebKit/537.36",
                        "Cookie": cookie_str,
                        "Content-Type": "application/x-www-form-urlencoded",
                    },
                )
                with opener.open(req_b, timeout=3) as res:
                    b_set = json.loads(res.read().decode("utf-8"))
                    if b_set.get("status") == "ok":
                        b_data = b_set.get("data", {})
                        daily_bonus_cnt = int(b_data.get("dailyBonusCnt", 0))
                        view_cur_day = int(b_data.get("viewCurDay", 0))
                        daily_bonus_claimed = daily_bonus_cnt > 0
                        daily_bonus_progress = f"{view_cur_day}/500"

                # Also fetch user balance
                req_u = urllib.request.Request(
                    "https://luckywatch.pro/api/user/",
                    data=urllib.parse.urlencode({"method": "getCurrentUser"}).encode("utf-8"),
                    headers={
                        "User-Agent": "Mozilla/5.0 (Linux; Android 14; Pixel 8 Pro) AppleWebKit/537.36",
                        "Cookie": cookie_str,
                        "Content-Type": "application/x-www-form-urlencoded",
                    },
                )
                with opener.open(req_u, timeout=3) as res_u:
                    u_res = json.loads(res_u.read().decode("utf-8"))
                    if u_res.get("status") == "ok" and "balance" in u_res.get("data", {}):
                        balance_val = str(u_res["data"]["balance"])
                        clovers_val = int(u_res["data"].get("clover", 0))

                _LAST_USER_FETCH[email] = {
                    "timestamp": now_ts,
                    "balance": balance_val,
                    "clovers": clovers_val,
                    "email_verified": email_verified,
                    "server_wallet_set": server_wallet_set,
                }
            except Exception:
                pass

        # Build current task structure
        curr_task = live_st.get("current_task")
        if curr_task:
            task_dict = {
                "id": curr_task.get("id"),
                "video_id": curr_task.get("video_id"),
                "duration": curr_task.get("duration"),
                "elapsed": min(curr_task.get("duration", 12), 12),
                "status": curr_task.get("status"),
            }
        else:
            task_dict = None

        # Fetch latest payout history item if available
        last_payout = None
        if cookie_str:
            try:
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url}))
                req_h = urllib.request.Request(
                    "https://luckywatch.pro/api/user/payout/",
                    data=urllib.parse.urlencode({"method": "history", "page": 1}).encode("utf-8"),
                    headers={"Cookie": cookie_str, "Content-Type": "application/x-www-form-urlencoded", "User-Agent": "Mozilla/5.0"},
                )
                with opener.open(req_h, timeout=2) as res_h:
                    h_data = json.loads(res_h.read().decode("utf-8")).get("data", {}).get("history", {}).get("data", [])
                    if h_data:
                        item = h_data[0]
                        # LuckyWatch Payout Status Mapping (from live /payouts?tab=history DOM inspection):
                        # status: "1" = PAID (Dibayar)
                        # status: "2" = IN PROGRESS / PROCESSING
                        # status: "0" = CANCELLED / ERROR
                        status_map = {
                            "1": "PAID",
                            "2": "IN PROGRESS",
                            "0": "PAYMENT ERROR"
                        }
                        last_payout = {
                            "id": item.get("id"),
                            "amount": item.get("val"),
                            "net_amount": item.get("commissionVal"),
                            "wallet": item.get("account"),
                            "status": status_map.get(str(item.get("status")), "IN PROGRESS"),
                            "timestamp": time.strftime("%Y-%m-%d %H:%M", time.localtime(int(item.get("unixtime", 0)))),
                        }
            except Exception:
                pass

        acc_obj = {
            "email": email,
            "email_redacted": redacted,
            "proxy": proxy_url,
            "faucetpay_usdt_trc20": acc.get("faucetpay_usdt_trc20", ""),
            "email_verified": email_verified,
            "server_wallet_set": server_wallet_set,
            "daily_bonus_claimed": daily_bonus_claimed,
            "daily_bonus_progress": daily_bonus_progress,
            "country": geo.get("country", "UN"),
            "country_name": geo.get("country_name", "Unknown"),
            "city": geo.get("city", "Unknown"),
            "egress_ip": geo.get("egress_ip", "Unknown"),
            "isp": geo.get("isp", "Unknown"),
            "status": live_st.get("status", "ACTIVE"),
            "balance": balance_val,
            "clovers": clovers_val,
            "daily_done": live_st.get("daily_done", 0),
            "daily_cap": live_st.get("daily_cap", 560),
            "hourly_done": live_st.get("hourly_done", 0),
            "hourly_cap": live_st.get("hourly_cap", 65),
            "current_task": task_dict,
            "last_payout": last_payout,
            "countdown_sleep": live_st.get("countdown_sleep", 0),
            "error_reason": live_st.get("error_reason"),
            "last_activity_time": live_st.get("last_activity_time", "-"),
        }
        accounts_list.append(acc_obj)
        # Match by prefix (e.g. 'halolakapa13' or 'halolakapa')
        matching_logs = raw_acc_logs.get(acc_prefix, [])
        if not matching_logs:
            for k, logs in raw_acc_logs.items():
                if k in acc_prefix or acc_prefix in k:
                    matching_logs = logs
                    break
        formatted_acc_logs[redacted] = matching_logs

    # Fleet-wide calculations
    total_balance = sum([float(a["balance"]) for a in accounts_list]) if accounts_list else 0.0
    total_clovers = sum([int(a["clovers"]) for a in accounts_list]) if accounts_list else 0
    total_tasks_today = sum([a["daily_done"] for a in accounts_list]) if accounts_list else 0

    active_workers = len([a for a in accounts_list if a["status"] == "ACTIVE"])
    sleeping_workers = len([a for a in accounts_list if a["status"] == "SLEEPING"])
    error_workers = len([a for a in accounts_list if a["status"] == "ERROR"])

    summary = {
        "total_balance": f"{total_balance:.7f}",
        "total_clovers": total_clovers,
        "total_accounts": len(accounts_list),
        "active_workers": active_workers,
        "sleeping_workers": sleeping_workers,
        "error_workers": error_workers,
        "total_tasks_today": total_tasks_today,
    }

    auto_cfg = {}
    if CONFIG_FILE.exists():
        try:
            cfg = json.loads(CONFIG_FILE.read_text())
            auto_cfg = cfg.get("auto_withdraw", {})
        except Exception:
            pass

    # Backward compatibility fields for legacy clients
    return {
        "email": f"{len(accounts_list)} Active Account(s)" if accounts_list else "Multi-Worker",
        "balance": f"{total_balance:.7f}",
        "clovers": str(total_clovers),
        "proxy": "Node 01 (ID) + Node 02 (SG)",
        "summary": summary,
        "accounts": accounts_list,
        "logs": tail_logs,
        "account_logs": formatted_acc_logs,
        "auto_withdraw": auto_cfg,
        "service_status": "RUNNING 24/7 (MULTI-THREAD)",
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />
  <title>LuckyWatch Fleet Telemetry & Withdrawal Hub</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&family=JetBrains+Mono:ital,wght@0,400;0,500;0,600;0,700;1,400&display=swap" rel="stylesheet">
  <style>
    :root {
      color-scheme: dark;
      --bg: #07090E;
      --bg-gradient: radial-gradient(circle at 50% 0%, #0F172A 0%, #07090E 70%);
      --surface-0: rgba(11, 15, 25, 0.7);
      --surface-1: rgba(18, 24, 38, 0.65);
      --surface-2: rgba(30, 41, 59, 0.55);
      --surface-glass: rgba(15, 23, 42, 0.6);
      --surface-glass-border: rgba(255, 255, 255, 0.08);
      --surface-glass-hover: rgba(30, 41, 59, 0.85);
      --border-subtle: rgba(255, 255, 255, 0.06);
      --border-medium: rgba(255, 255, 255, 0.12);
      
      --emerald: #10B981;
      --emerald-bright: #34D399;
      --emerald-glow: rgba(16, 185, 129, 0.25);
      --emerald-subtle: rgba(16, 185, 129, 0.12);
      
      --cyan: #38BDF8;
      --cyan-glow: rgba(56, 189, 248, 0.25);
      
      --amber: #F59E0B;
      --amber-bright: #FBBF24;
      --amber-glow: rgba(245, 158, 11, 0.25);
      
      --rose: #F43F5E;
      --rose-bright: #FB7185;
      --rose-glow: rgba(244, 63, 94, 0.25);
      
      --gold: #EAB308;
      --gold-gradient: linear-gradient(135deg, #FDE047 0%, #CA8A04 100%);
      --gold-glow: rgba(234, 179, 8, 0.35);

      --text-main: #F8FAFC;
      --text-secondary: #94A3B8;
      --text-tertiary: #64748B;
      --text-code: #E2E8F0;

      --radius-sm: 8px;
      --radius-md: 12px;
      --radius-lg: 16px;
      --radius-xl: 20px;
      --radius-full: 9999px;
      
      --shadow-hud: 0 8px 32px 0 rgba(0, 0, 0, 0.45), inset 0 1px 0 0 rgba(255, 255, 255, 0.1);
      --shadow-card: 0 4px 20px 0 rgba(0, 0, 0, 0.35), inset 0 1px 0 0 rgba(255, 255, 255, 0.05);
      --shadow-glow: 0 0 24px -4px var(--emerald-glow);
    }

    * { box-sizing: border-box; margin: 0; padding: 0; }
    
    body {
      background: var(--bg);
      background-image: var(--bg-gradient);
      background-attachment: fixed;
      color: var(--text-main);
      font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
      min-height: 100vh;
      padding: 24px 16px 40px;
      display: flex;
      flex-direction: column;
      align-items: center;
      -webkit-font-smoothing: antialiased;
      -moz-osx-font-smoothing: grayscale;
      overflow-x: hidden;
    }

    .app-viewport {
      width: 100%;
      max-width: 1280px;
      display: flex;
      flex-direction: column;
      gap: 24px;
    }

    /* TYPOGRAPHY UTILS */
    .mono { font-family: 'JetBrains Mono', monospace; font-feature-settings: "tnum" 1, "zero" 1; }
    .tabular { font-variant-numeric: tabular-nums; }
    
    /* GLASS CONTAINERS */
    .glass-panel {
      background: var(--surface-glass);
      backdrop-filter: blur(24px) saturate(180%);
      -webkit-backdrop-filter: blur(24px) saturate(180%);
      border: 1px solid var(--surface-glass-border);
      border-radius: var(--radius-lg);
      box-shadow: var(--shadow-hud);
    }

    /* HEADER & BRAND */
    .header-bar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      flex-wrap: wrap;
      gap: 16px;
      padding: 16px 22px;
    }

    .brand-cluster {
      display: flex;
      align-items: center;
      gap: 14px;
    }

    .brand-mark {
      width: 44px;
      height: 44px;
      border-radius: var(--radius-md);
      background: linear-gradient(135deg, rgba(16, 185, 129, 0.25) 0%, rgba(5, 150, 105, 0.35) 100%);
      border: 1px solid rgba(52, 211, 153, 0.4);
      display: flex;
      align-items: center;
      justify-content: center;
      box-shadow: 0 4px 16px rgba(16, 185, 129, 0.25), inset 0 1px 0 rgba(255, 255, 255, 0.2);
      position: relative;
    }

    .brand-mark svg {
      width: 24px;
      height: 24px;
      fill: none;
      stroke: var(--emerald-bright);
      stroke-width: 2.2;
      stroke-linecap: round;
      stroke-linejoin: round;
    }

    .brand-text h1 {
      font-size: 18px;
      font-weight: 800;
      letter-spacing: -0.03em;
      color: #FFFFFF;
      display: flex;
      align-items: center;
      gap: 8px;
    }

    .brand-text p {
      font-size: 12px;
      color: var(--text-secondary);
      font-weight: 500;
      margin-top: 2px;
    }

    .header-controls {
      display: flex;
      align-items: center;
      gap: 10px;
      flex-wrap: wrap;
    }

    .btn-action {
      background: rgba(255, 255, 255, 0.04);
      border: 1px solid var(--border-medium);
      color: var(--text-main);
      padding: 8px 14px;
      border-radius: var(--radius-sm);
      font-size: 12px;
      font-weight: 600;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      gap: 6px;
      transition: all 0.2s cubic-bezier(0.4, 0, 0.2, 1);
      user-select: none;
    }

    .btn-action:hover {
      background: rgba(255, 255, 255, 0.08);
      border-color: rgba(255, 255, 255, 0.2);
      transform: translateY(-1px);
    }

    .btn-action:active {
      transform: translateY(0);
    }

    .btn-action svg {
      width: 14px;
      height: 14px;
      stroke: currentColor;
      stroke-width: 2;
      fill: none;
    }

    .live-status-pill {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      background: rgba(16, 185, 129, 0.1);
      border: 1px solid rgba(16, 185, 129, 0.3);
      color: var(--emerald-bright);
      padding: 6px 12px;
      border-radius: var(--radius-full);
      font-size: 11.5px;
      font-weight: 700;
      letter-spacing: 0.04em;
    }

    .pulse-dot {
      width: 8px;
      height: 8px;
      background: var(--emerald);
      border-radius: 50%;
      position: relative;
    }

    .pulse-dot::after {
      content: '';
      position: absolute;
      inset: -4px;
      border-radius: 50%;
      background: var(--emerald);
      opacity: 0.6;
      animation: ripple 2s infinite cubic-bezier(0.215, 0.61, 0.355, 1);
    }

    @keyframes ripple {
      0% { transform: scale(0.8); opacity: 0.8; }
      100% { transform: scale(2.4); opacity: 0; }
    }

    /* GLOBAL FLEET SUMMARY HUD */
    .hud-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
      gap: 16px;
    }

    .hud-card {
      background: var(--surface-1);
      border: 1px solid var(--border-subtle);
      border-radius: var(--radius-lg);
      padding: 18px 20px;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      position: relative;
      overflow: hidden;
      box-shadow: var(--shadow-card);
      transition: border-color 0.2s, transform 0.2s;
    }

    .hud-card:hover {
      border-color: var(--border-medium);
      transform: translateY(-1px);
    }

    .hud-card-top {
      display: flex;
      align-items: center;
      justify-content: space-between;
      margin-bottom: 10px;
    }

    .hud-label {
      font-size: 11.5px;
      font-weight: 600;
      color: var(--text-secondary);
      text-transform: uppercase;
      letter-spacing: 0.06em;
      display: flex;
      align-items: center;
      gap: 6px;
    }

    .hud-icon-badge {
      width: 28px;
      height: 28px;
      border-radius: var(--radius-sm);
      display: flex;
      align-items: center;
      justify-content: center;
      background: rgba(255, 255, 255, 0.04);
      border: 1px solid var(--border-subtle);
    }

    .hud-icon-badge svg {
      width: 15px;
      height: 15px;
      stroke: var(--text-secondary);
      stroke-width: 2;
      fill: none;
    }

    .hud-val-large {
      font-size: 26px;
      font-weight: 800;
      letter-spacing: -0.02em;
      line-height: 1.1;
      margin-bottom: 6px;
    }

    .hud-sub {
      font-size: 12px;
      color: var(--text-tertiary);
      display: flex;
      align-items: center;
      gap: 6px;
      font-weight: 500;
    }

    /* WITHDRAWAL READINESS HUB */
    .withdraw-hub {
      background: linear-gradient(180deg, rgba(30, 41, 59, 0.5) 0%, rgba(15, 23, 42, 0.7) 100%);
      border: 1px solid var(--border-medium);
      border-radius: var(--radius-xl);
      padding: 22px 26px;
      display: flex;
      flex-direction: column;
      gap: 20px;
      box-shadow: var(--shadow-hud);
      position: relative;
    }

    .withdraw-hub-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      flex-wrap: wrap;
      gap: 16px;
    }

    .hub-title-area {
      display: flex;
      flex-direction: column;
      gap: 4px;
    }

    .hub-title-area h2 {
      font-size: 16px;
      font-weight: 700;
      color: #FFFFFF;
      display: flex;
      align-items: center;
      gap: 8px;
    }

    .hub-title-area p {
      font-size: 12px;
      color: var(--text-secondary);
    }

    .threshold-selector {
      display: flex;
      align-items: center;
      gap: 8px;
      background: rgba(0, 0, 0, 0.35);
      padding: 4px;
      border-radius: var(--radius-md);
      border: 1px solid var(--border-subtle);
      flex-wrap: wrap;
    }

    .preset-pill {
      background: transparent;
      border: none;
      color: var(--text-secondary);
      font-size: 12px;
      font-weight: 600;
      padding: 6px 12px;
      border-radius: 6px;
      cursor: pointer;
      transition: all 0.15s;
    }

    .preset-pill:hover {
      color: var(--text-main);
      background: rgba(255, 255, 255, 0.05);
    }

    .preset-pill.active {
      background: rgba(16, 185, 129, 0.2);
      color: var(--emerald-bright);
      border: 1px solid rgba(16, 185, 129, 0.4);
      box-shadow: 0 2px 8px rgba(16, 185, 129, 0.2);
    }

    .custom-threshold-wrap {
      display: flex;
      align-items: center;
      background: rgba(255, 255, 255, 0.04);
      border: 1px solid var(--border-medium);
      border-radius: 6px;
      padding: 2px 8px;
      gap: 4px;
    }

    .custom-threshold-wrap span {
      font-size: 12px;
      color: var(--text-tertiary);
      font-family: 'JetBrains Mono', monospace;
    }

    .custom-threshold-input {
      background: transparent;
      border: none;
      outline: none;
      color: #FFFFFF;
      font-family: 'JetBrains Mono', monospace;
      font-size: 12px;
      font-weight: 600;
      width: 58px;
    }

    .withdraw-overview-metrics {
      display: grid;
      grid-template-columns: 1.4fr 1fr 1fr;
      gap: 16px;
      align-items: center;
      background: rgba(0, 0, 0, 0.25);
      border: 1px solid var(--border-subtle);
      border-radius: var(--radius-lg);
      padding: 16px 20px;
    }

    @media (max-width: 860px) {
      .withdraw-overview-metrics {
        grid-template-columns: 1fr;
        gap: 16px;
      }
    }

    .global-progress-block {
      display: flex;
      flex-direction: column;
      gap: 8px;
    }

    .progress-bar-wrapper {
      position: relative;
      height: 10px;
      background: rgba(255, 255, 255, 0.06);
      border-radius: var(--radius-full);
      overflow: hidden;
      border: 1px solid rgba(255, 255, 255, 0.04);
    }

    .progress-bar-inner {
      height: 100%;
      border-radius: var(--radius-full);
      background: linear-gradient(90deg, #10B981, #38BDF8);
      transition: width 0.4s cubic-bezier(0.4, 0, 0.2, 1);
      position: relative;
    }

    .progress-bar-inner.gold-tier {
      background: var(--gold-gradient);
      box-shadow: 0 0 12px var(--gold-glow);
    }

    .status-gold-badge {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 6px 12px;
      background: rgba(234, 179, 8, 0.15);
      border: 1px solid rgba(234, 179, 8, 0.4);
      color: #FDE047;
      border-radius: var(--radius-full);
      font-size: 12px;
      font-weight: 700;
      letter-spacing: 0.03em;
    }

    /* CONTROLS BAR (FILTERS & VIEW SWITCHER) */
    .controls-bar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      flex-wrap: wrap;
      gap: 12px;
      padding: 12px 18px;
    }

    .filter-tabs {
      display: flex;
      align-items: center;
      gap: 6px;
      flex-wrap: wrap;
    }

    .filter-tab-btn {
      background: transparent;
      border: 1px solid transparent;
      color: var(--text-secondary);
      font-size: 12px;
      font-weight: 600;
      padding: 6px 12px;
      border-radius: var(--radius-sm);
      cursor: pointer;
      transition: all 0.15s;
    }

    .filter-tab-btn:hover {
      color: var(--text-main);
      background: rgba(255, 255, 255, 0.04);
    }

    .filter-tab-btn.active {
      background: rgba(255, 255, 255, 0.08);
      border-color: var(--border-medium);
      color: #FFFFFF;
    }

    .right-tools {
      display: flex;
      align-items: center;
      gap: 10px;
    }

    .search-input-wrap {
      display: flex;
      align-items: center;
      background: rgba(0, 0, 0, 0.3);
      border: 1px solid var(--border-subtle);
      border-radius: var(--radius-sm);
      padding: 6px 10px;
      gap: 6px;
      width: 200px;
      transition: border-color 0.2s, width 0.2s;
    }

    .search-input-wrap:focus-within {
      border-color: var(--cyan);
      width: 240px;
    }

    .search-input-wrap svg {
      width: 14px;
      height: 14px;
      stroke: var(--text-tertiary);
      stroke-width: 2;
      fill: none;
    }

    .search-input-wrap input {
      background: transparent;
      border: none;
      outline: none;
      color: #FFFFFF;
      font-size: 12px;
      width: 100%;
    }

    .view-toggle {
      display: flex;
      align-items: center;
      background: rgba(0, 0, 0, 0.4);
      border: 1px solid var(--border-subtle);
      border-radius: var(--radius-sm);
      padding: 2px;
    }

    .view-btn {
      background: transparent;
      border: none;
      color: var(--text-tertiary);
      padding: 6px 8px;
      border-radius: 4px;
      cursor: pointer;
      display: flex;
      align-items: center;
      justify-content: center;
      transition: all 0.15s;
    }

    .view-btn svg {
      width: 15px;
      height: 15px;
      stroke: currentColor;
      stroke-width: 2;
      fill: none;
    }

    .view-btn.active {
      background: rgba(255, 255, 255, 0.1);
      color: #FFFFFF;
    }

    /* ACCOUNTS CONTAINER (CARD GRID & LEDGER MATRIX) */
    .accounts-grid-view {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(360px, 1fr));
      gap: 16px;
    }

    .account-card {
      background: var(--surface-1);
      border: 1px solid var(--border-subtle);
      border-radius: var(--radius-lg);
      padding: 18px 20px;
      display: flex;
      flex-direction: column;
      gap: 14px;
      box-shadow: var(--shadow-card);
      position: relative;
      overflow: hidden;
      transition: border-color 0.2s, transform 0.2s;
    }

    .account-card:hover {
      border-color: var(--border-medium);
      transform: translateY(-2px);
    }

    .account-card.ready-withdraw-border {
      border-color: rgba(234, 179, 8, 0.5);
      box-shadow: 0 4px 20px rgba(234, 179, 8, 0.15);
    }

    .card-top-strip {
      display: flex;
      align-items: center;
      justify-content: space-between;
    }

    .acc-id-cluster {
      display: flex;
      align-items: center;
      gap: 10px;
    }

    .acc-avatar {
      width: 32px;
      height: 32px;
      border-radius: 50%;
      background: linear-gradient(135deg, rgba(56, 189, 248, 0.2), rgba(16, 185, 129, 0.2));
      border: 1px solid rgba(255, 255, 255, 0.1);
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 13px;
      font-weight: 700;
      color: #FFFFFF;
      font-family: 'JetBrains Mono', monospace;
    }

    .acc-email-box {
      display: flex;
      flex-direction: column;
    }

    .email-label {
      font-size: 13.5px;
      font-weight: 700;
      color: #FFFFFF;
      font-family: 'JetBrains Mono', monospace;
      display: flex;
      align-items: center;
      gap: 6px;
    }

    .copy-btn {
      background: transparent;
      border: none;
      color: var(--text-tertiary);
      cursor: pointer;
      padding: 2px;
      display: inline-flex;
      align-items: center;
      transition: color 0.15s;
    }

    .copy-btn:hover {
      color: var(--cyan);
    }

    .copy-btn svg {
      width: 13px;
      height: 13px;
      stroke: currentColor;
      stroke-width: 2;
      fill: none;
    }

    .proxy-flag-chip {
      font-size: 11px;
      color: var(--text-secondary);
      display: flex;
      align-items: center;
      gap: 4px;
      margin-top: 1px;
    }

    .status-badge-chip {
      font-size: 11px;
      font-weight: 700;
      padding: 4px 10px;
      border-radius: var(--radius-full);
      text-transform: uppercase;
      letter-spacing: 0.04em;
      display: inline-flex;
      align-items: center;
      gap: 6px;
    }

    .chip-active {
      background: var(--emerald-subtle);
      border: 1px solid rgba(16, 185, 129, 0.3);
      color: var(--emerald-bright);
    }

    .chip-sleeping {
      background: rgba(245, 158, 11, 0.12);
      border: 1px solid rgba(245, 158, 11, 0.3);
      color: var(--amber-bright);
    }

    .chip-error {
      background: rgba(244, 63, 94, 0.12);
      border: 1px solid rgba(244, 63, 94, 0.3);
      color: var(--rose-bright);
    }

    .balances-row {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 10px;
    }

    .balance-box {
      background: rgba(0, 0, 0, 0.3);
      border: 1px solid var(--border-subtle);
      border-radius: var(--radius-sm);
      padding: 10px 12px;
    }

    .balance-box .b-label {
      font-size: 10.5px;
      color: var(--text-tertiary);
      text-transform: uppercase;
      font-weight: 600;
      letter-spacing: 0.05em;
      margin-bottom: 2px;
    }

    .balance-box .b-val {
      font-size: 16px;
      font-weight: 700;
      font-family: 'JetBrains Mono', monospace;
    }

    /* CARD PROGRESS SECTION */
    .card-quotas {
      display: flex;
      flex-direction: column;
      gap: 10px;
      background: rgba(0, 0, 0, 0.2);
      border-radius: var(--radius-sm);
      padding: 10px 12px;
      border: 1px solid var(--border-subtle);
    }

    .quota-item {
      display: flex;
      flex-direction: column;
      gap: 4px;
    }

    .quota-header {
      display: flex;
      justify-content: space-between;
      font-size: 11px;
      color: var(--text-secondary);
      font-weight: 500;
    }

    .mini-progress {
      height: 6px;
      background: rgba(255, 255, 255, 0.06);
      border-radius: var(--radius-full);
      overflow: hidden;
    }

    .mini-progress-fill {
      height: 100%;
      border-radius: var(--radius-full);
      background: linear-gradient(90deg, #10B981, #38BDF8);
      transition: width 0.3s ease;
    }

    .activity-live-box {
      font-size: 11.5px;
      background: rgba(0, 0, 0, 0.35);
      padding: 8px 12px;
      border-radius: var(--radius-sm);
      border: 1px solid var(--border-subtle);
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
    }

    .activity-content {
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }

    .card-footer-actions {
      display: flex;
      align-items: center;
      justify-content: space-between;
      font-size: 11px;
      color: var(--text-tertiary);
      border-top: 1px solid var(--border-subtle);
      padding-top: 10px;
    }

    /* DENSE MATRIX TABLE VIEW */
    .matrix-table-wrap {
      width: 100%;
      overflow-x: auto;
      border-radius: var(--radius-lg);
      border: 1px solid var(--border-subtle);
      background: var(--surface-1);
    }

    .matrix-table {
      width: 100%;
      border-collapse: collapse;
      font-size: 12.5px;
      text-align: left;
    }

    .matrix-table th {
      background: rgba(0, 0, 0, 0.4);
      padding: 12px 16px;
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      color: var(--text-tertiary);
      font-weight: 700;
      border-bottom: 1px solid var(--border-medium);
      white-space: nowrap;
    }

    .matrix-table td {
      padding: 14px 16px;
      border-bottom: 1px solid var(--border-subtle);
      color: var(--text-main);
      vertical-align: middle;
      white-space: nowrap;
    }

    .matrix-table tr:last-child td {
      border-bottom: none;
    }

    .matrix-table tr:hover td {
      background: rgba(255, 255, 255, 0.02);
    }

    /* TERMINAL LOG STREAM PANEL */
    .terminal-panel {
      background: #05070B;
      border: 1px solid var(--border-medium);
      border-radius: var(--radius-lg);
      padding: 20px;
      display: flex;
      flex-direction: column;
      gap: 14px;
      box-shadow: var(--shadow-hud);
    }

    .terminal-top-nav {
      display: flex;
      align-items: center;
      justify-content: space-between;
      flex-wrap: wrap;
      gap: 12px;
      border-bottom: 1px solid var(--border-subtle);
      padding-bottom: 12px;
    }

    .terminal-tabs {
      display: flex;
      align-items: center;
      gap: 6px;
      flex-wrap: wrap;
    }

    .t-tab-btn {
      background: rgba(255, 255, 255, 0.03);
      border: 1px solid var(--border-subtle);
      color: var(--text-secondary);
      padding: 6px 12px;
      border-radius: var(--radius-sm);
      font-size: 11.5px;
      cursor: pointer;
      font-family: 'JetBrains Mono', monospace;
      font-weight: 600;
      transition: all 0.15s;
    }

    .t-tab-btn:hover {
      background: rgba(255, 255, 255, 0.06);
      color: #FFFFFF;
    }

    .t-tab-btn.active {
      background: rgba(56, 189, 248, 0.15);
      border-color: rgba(56, 189, 248, 0.4);
      color: var(--cyan);
    }

    .terminal-tools {
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
    }

    .log-level-select {
      background: rgba(0, 0, 0, 0.4);
      border: 1px solid var(--border-subtle);
      color: var(--text-secondary);
      font-size: 11px;
      font-family: 'JetBrains Mono', monospace;
      padding: 4px 8px;
      border-radius: 4px;
      outline: none;
    }

    .terminal-view-window {
      background: #020306;
      border: 1px solid rgba(255, 255, 255, 0.04);
      border-radius: var(--radius-md);
      padding: 14px 16px;
      height: 380px;
      overflow-y: auto;
      font-family: 'JetBrains Mono', monospace;
      font-size: 11.5px;
      line-height: 1.6;
      color: var(--text-code);
      position: relative;
    }

    .t-log-entry {
      padding: 2px 0;
      border-bottom: 1px solid rgba(255, 255, 255, 0.015);
      display: flex;
      gap: 8px;
      word-break: break-all;
    }

    .t-log-time { color: var(--text-tertiary); user-select: none; }
    .t-log-tag { color: var(--cyan); font-weight: 600; }
    .t-log-success { color: var(--emerald-bright); }
    .t-log-warn { color: var(--amber-bright); }
    .t-log-error { color: var(--rose-bright); }
    .t-log-info { color: var(--text-secondary); }

    /* TOAST FEEDBACK */
    .toast-container {
      position: fixed;
      bottom: 24px;
      right: 24px;
      z-index: 1000;
      display: flex;
      flex-direction: column;
      gap: 8px;
    }

    .toast-msg {
      background: rgba(15, 23, 42, 0.95);
      border: 1px solid var(--emerald);
      color: #FFFFFF;
      padding: 10px 16px;
      border-radius: var(--radius-sm);
      font-size: 12px;
      box-shadow: 0 4px 20px rgba(0,0,0,0.5);
      display: flex;
      align-items: center;
      gap: 8px;
      animation: slideIn 0.2s cubic-bezier(0.4, 0, 0.2, 1);
    }

    @keyframes slideIn {
      from { transform: translateX(100%); opacity: 0; }
      to { transform: translateX(0); opacity: 1; }
    }

    /* SCROLLBAR REFINEMENT */
    ::-webkit-scrollbar { width: 6px; height: 6px; }
    ::-webkit-scrollbar-track { background: transparent; }
    ::-webkit-scrollbar-thumb { background: rgba(255, 255, 255, 0.12); border-radius: 3px; }
    ::-webkit-scrollbar-thumb:hover { background: rgba(255, 255, 255, 0.2); }

    /* RESPONSIVE BREAKPOINTS */
    @media (max-width: 640px) {
      body { padding: 12px 8px 30px; }
      .brand-text h1 { font-size: 16px; }
      .hud-grid { grid-template-columns: 1fr; }
      .accounts-grid-view { grid-template-columns: 1fr; }
      .search-input-wrap { width: 100%; }
      .search-input-wrap:focus-within { width: 100%; }
      .controls-bar { flex-direction: column; align-items: stretch; }
      .right-tools { justify-content: space-between; }
    }
  </style>
</head>
<body>
  <div class="app-viewport">
    
    <!-- HEADER BAR -->
    <header class="glass-panel header-bar">
      <div class="brand-cluster">
        <div class="brand-mark">
          <svg viewBox="0 0 24 24"><path d="M12 2v20M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"/></svg>
        </div>
        <div class="brand-text">
          <h1>LuckyWatch Fleet Telemetry</h1>
          <p>High-Concurrency Multi-Account Video Stream Engine • 24/7 Autopilot</p>
        </div>
      </div>
      
      <div class="header-controls">
        <button class="btn-action" onclick="triggerAction('retry')">
          <svg viewBox="0 0 24 24"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg>
          Restart Daemon
        </button>
        <button class="btn-action" onclick="triggerAction('refresh')">
          <svg viewBox="0 0 24 24"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg>
          Force Cache Purge
        </button>
        <div class="live-status-pill">
          <div class="pulse-dot"></div>
          <span id="service-status-text">FLEET RUNNING 24/7</span>
        </div>
      </div>
    </header>

    <!-- GLOBAL FLEET SUMMARY HUD -->
    <section class="hud-grid">
      <div class="hud-card">
        <div class="hud-card-top">
          <span class="hud-label">
            <svg style="width:14px; height:14px; stroke:var(--emerald-bright); fill:none;" viewBox="0 0 24 24"><line x1="12" y1="1" x2="12" y2="23"/><path d="M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"/></svg>
            Combined Fleet Balance
          </span>
          <div class="hud-icon-badge">
            <svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><path d="M16 8h-6a2 2 0 1 0 0 4h4a2 2 0 1 1 0 4H8"/><line x1="12" y1="6" x2="12" y2="8"/><line x1="12" y1="16" x2="12" y2="18"/></svg>
          </div>
        </div>
        <div class="hud-val-large mono tabular" id="hud-total-balance" style="color: var(--emerald-bright);">$0.0000000</div>
        <div class="hud-sub" id="hud-total-accounts">2 Active Dedicated Nodes</div>
      </div>

      <div class="hud-card">
        <div class="hud-card-top">
          <span class="hud-label">
            <svg style="width:14px; height:14px; stroke:var(--amber-bright); fill:none;" viewBox="0 0 24 24"><polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/></svg>
            Total Clovers
          </span>
          <div class="hud-icon-badge">
            <svg viewBox="0 0 24 24"><path d="M12 2l3 7h7l-5.5 4.5 2 7.5L12 17l-6.5 4 2-7.5L2 9h7z"/></svg>
          </div>
        </div>
        <div class="hud-val-large mono tabular" id="hud-total-clovers" style="color: var(--amber-bright);">0</div>
        <div class="hud-sub">Accumulated Multipliers</div>
      </div>

      <div class="hud-card">
        <div class="hud-card-top">
          <span class="hud-label">
            <svg style="width:14px; height:14px; stroke:var(--cyan); fill:none;" viewBox="0 0 24 24"><rect x="2" y="2" width="20" height="8" rx="2" ry="2"/><rect x="2" y="14" width="20" height="8" rx="2" ry="2"/><line x1="6" y1="6" x2="6.01" y2="6"/><line x1="6" y1="18" x2="6.01" y2="18"/></svg>
            Active Fleet Workers
          </span>
          <div class="hud-icon-badge">
            <svg viewBox="0 0 24 24"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>
          </div>
        </div>
        <div class="hud-val-large mono tabular" id="hud-worker-status" style="font-size: 22px;">0 Active / 0 Sleep</div>
        <div class="hud-sub" id="hud-sync-time">Polling live telemetry...</div>
      </div>

      <div class="hud-card">
        <div class="hud-card-top">
          <span class="hud-label">
            <svg style="width:14px; height:14px; stroke:#A855F7; fill:none;" viewBox="0 0 24 24"><polygon points="5 3 19 12 5 21 5 3"/></svg>
            Daily Videos Claimed
          </span>
          <div class="hud-icon-badge">
            <svg viewBox="0 0 24 24"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>
          </div>
        </div>
        <div class="hud-val-large mono tabular" id="hud-total-tasks" style="color: #C084FC;">0</div>
        <div class="hud-sub">Combined 24h Yield</div>
      </div>
    </section>

    <!-- WITHDRAWAL READINESS HUB -->
    <section class="withdraw-hub">
      <div class="withdraw-hub-header">
        <div class="hub-title-area">
          <h2>
            <svg style="width:18px; height:18px; stroke:var(--amber-bright); fill:none;" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><path d="m9 12 2 2 4-4"/></svg>
            Withdrawal Readiness Hub
          </h2>
          <p>Real-time payout target tracking with dynamic threshold calculation per account & fleet</p>
        </div>

        <div class="threshold-selector" style="display:flex; align-items:center; gap:8px; flex-wrap:wrap;">
          <span style="font-size: 11px; font-weight: 700; color: var(--text-tertiary); text-transform: uppercase; padding: 0 4px;">Target:</span>
          <button class="preset-pill" onclick="setThreshold(0.10)" id="pill-010">$0.10</button>
          <button class="preset-pill" onclick="setThreshold(0.50)" id="pill-050">$0.50</button>
          <button class="preset-pill" onclick="setThreshold(1.00)" id="pill-100">$1.00</button>
          <button class="preset-pill" onclick="setThreshold(5.00)" id="pill-500">$5.00</button>
          <div class="custom-threshold-wrap">
            <span>$</span>
            <input type="number" step="0.05" min="0.01" max="100" class="custom-threshold-input" id="custom-threshold-input" placeholder="Custom" onchange="handleCustomThreshold(this.value)" />
          </div>
          <div style="border-left: 1px solid var(--border-subtle); padding-left: 8px; margin-left: 4px; display:flex; align-items:center; gap:6px;">
            <span style="font-size: 11px; font-weight: 700; color: var(--text-secondary); text-transform: uppercase;">⚡ Auto:</span>
            <button class="btn-action" id="btn-toggle-auto-withdraw" onclick="toggleAutoWithdrawConfig()" style="font-size: 10.5px; padding: 4px 8px; border-radius: 6px; font-weight: 700;">
              ON ($0.10)
            </button>
          </div>
        </div>
      </div>

      <div class="withdraw-overview-metrics">
        <div class="global-progress-block">
          <div style="display: flex; justify-content: space-between; font-size: 12px; font-weight: 600;">
            <span style="color: var(--text-secondary);">Combined Fleet Progress to Target</span>
            <span class="mono tabular" id="global-progress-pct" style="color: var(--emerald-bright);">0%</span>
          </div>
          <div class="progress-bar-wrapper">
            <div class="progress-bar-inner" id="global-progress-fill" style="width: 0%;"></div>
          </div>
          <div style="display: flex; justify-content: space-between; font-size: 11px; color: var(--text-tertiary);" class="mono">
            <span id="global-current-num">$0.0000000 USD</span>
            <span id="global-target-num">Target: $0.10 USD</span>
          </div>
        </div>

        <div style="display: flex; flex-direction: column; gap: 4px; padding-left: 10px; border-left: 1px solid var(--border-subtle);">
          <span style="font-size: 11px; color: var(--text-tertiary); text-transform: uppercase; font-weight: 700;">Readiness Verdict</span>
          <div id="readiness-verdict-box">
            <div class="status-gold-badge">
              <div class="pulse-dot" style="background: #FDE047;"></div>
              <span>CALCULATING...</span>
            </div>
          </div>
        </div>

        <div style="display: flex; flex-direction: column; gap: 4px; padding-left: 10px; border-left: 1px solid var(--border-subtle);">
          <span style="font-size: 11px; color: var(--text-tertiary); text-transform: uppercase; font-weight: 700;">Accounts at Goal</span>
          <div class="mono tabular" id="accounts-ready-count" style="font-size: 20px; font-weight: 800; color: #FFFFFF;">0 / 2 Accounts</div>
        </div>

        <div style="display: flex; align-items: center; padding-left: 10px; border-left: 1px solid var(--border-subtle);">
          <button class="btn-action" id="btn-withdraw-all" style="background: var(--gold-gradient); color: #000; font-weight: 800; font-size: 12px; padding: 8px 14px; border: none; box-shadow: 0 0 15px var(--gold-glow);" onclick="triggerWithdrawAll()">
            ⚡ Auto Withdraw All Ready
          </button>
        </div>
      </div>
    </section>

    <!-- VIEW CONTROLS & FILTER BAR -->
    <section class="glass-panel controls-bar">
      <div class="filter-tabs">
        <button class="filter-tab-btn active" onclick="setAccountFilter('all', this)">All Accounts (<span id="count-all">0</span>)</button>
        <button class="filter-tab-btn" onclick="setAccountFilter('active', this)">Active 🟢 (<span id="count-active">0</span>)</button>
        <button class="filter-tab-btn" onclick="setAccountFilter('sleeping', this)">Sleeping 🟡 (<span id="count-sleeping">0</span>)</button>
        <button class="filter-tab-btn" onclick="setAccountFilter('ready', this)">Ready to Withdraw 🏆 (<span id="count-ready">0</span>)</button>
        <button class="filter-tab-btn" onclick="setAccountFilter('error', this)">Error 🔴 (<span id="count-error">0</span>)</button>
      </div>

      <div class="right-tools">
        <div class="search-input-wrap">
          <svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
          <input type="text" id="acc-search-input" placeholder="Search account or IP..." oninput="handleSearch(this.value)" />
        </div>

        <div class="view-toggle">
          <button class="view-btn active" id="view-btn-grid" onclick="setViewMode('grid')" title="Card Grid View">
            <svg viewBox="0 0 24 24"><rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/></svg>
          </button>
          <button class="view-btn" id="view-btn-matrix" onclick="setViewMode('matrix')" title="Dense Ledger Matrix">
            <svg viewBox="0 0 24 24"><line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/><line x1="3" y1="6" x2="3.01" y2="6"/><line x1="3" y1="12" x2="3.01" y2="12"/><line x1="3" y1="18" x2="3.01" y2="18"/></svg>
          </button>
        </div>
      </div>
    </section>

    <!-- ACCOUNTS DISPLAY AREA -->
    <main id="accounts-display-container">
      <div class="accounts-grid-view" id="accounts-grid">
        <!-- Rendered dynamically -->
      </div>
      <div class="matrix-table-wrap" id="accounts-matrix" style="display: none;">
        <table class="matrix-table">
          <thead>
            <tr>
              <th>Worker Node</th>
              <th>Status</th>
              <th>Proxy Location & IP</th>
              <th>Balance (USD)</th>
              <th>Clovers</th>
              <th>Withdraw Goal</th>
              <th>Daily Quota</th>
              <th>Hourly Progress</th>
              <th>Live Activity</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody id="matrix-tbody">
            <!-- Rendered dynamically -->
          </tbody>
        </table>
      </div>
    </main>

    <!-- MULTI-CHANNEL LOG TERMINAL -->
    <section class="terminal-panel">
      <div class="terminal-top-nav">
        <div class="terminal-tabs" id="terminal-channel-tabs">
          <button class="t-tab-btn active" onclick="setLogChannel('all', this)">⚡ ALL FLEET LOGS</button>
          <!-- Dynamic account tabs -->
        </div>

        <div class="terminal-tools">
          <select class="log-level-select" id="log-level-filter" onchange="handleLogLevelChange(this.value)">
            <option value="ALL">ALL LEVELS</option>
            <option value="SUCCESS">SUCCESS ONLY</option>
            <option value="WARN">WARNINGS</option>
            <option value="ERROR">ERRORS</option>
          </select>

          <button class="btn-action" style="padding: 4px 8px; font-size: 11px;" onclick="toggleAutoScroll()" id="btn-autoscroll">
            🔒 Auto-Scroll: ON
          </button>
          <button class="btn-action" style="padding: 4px 8px; font-size: 11px;" onclick="clearTerminal()">
            🧹 Clear
          </button>
          <button class="btn-action" style="padding: 4px 8px; font-size: 11px;" onclick="exportLogs()">
            💾 Export
          </button>
        </div>
      </div>

      <div class="terminal-view-window" id="terminal-stream-window">
        <div style="color: var(--text-tertiary);">Connecting to LuckyWatch telemetry stream...</div>
      </div>
    </section>

  </div>

  <!-- WALLET CONFIG MODAL -->
  <div id="wallet-modal" style="display:none; position:fixed; inset:0; background:rgba(0,0,0,0.75); backdrop-filter:blur(10px); z-index:999; align-items:center; justify-content:center;">
    <div class="glass-panel" style="width:100%; max-width:480px; padding:24px; display:flex; flex-direction:column; gap:16px; border:1px solid var(--border-focus); box-shadow:0 20px 50px rgba(0,0,0,0.8);">
      <div style="display:flex; justify-content:space-between; align-items:center;">
        <h3 style="font-size:16px; font-weight:800; color:#fff; display:flex; align-items:center; gap:8px;">
          <span>⚙️</span> FaucetPay USDT TRC20 Setup
        </h3>
        <button onclick="closeWalletModal()" style="background:none; border:none; color:var(--text-tertiary); font-size:20px; cursor:pointer;">&times;</button>
      </div>

      <div style="display:flex; flex-direction:column; gap:6px;">
        <span style="font-size:12px; color:var(--text-secondary);">Target Account:</span>
        <div class="mono" id="modal-wallet-email" style="font-size:13px; font-weight:700; color:var(--cyan); padding:8px 12px; background:rgba(0,0,0,0.4); border-radius:8px; border:1px solid var(--border-subtle);">user@example.com</div>
      </div>

      <div style="display:flex; flex-direction:column; gap:6px;">
        <label style="font-size:12px; color:var(--text-secondary);">FaucetPay USDT (TRC20) Wallet Address:</label>
        <input type="text" id="modal-wallet-address" placeholder="T... (e.g. TVneJHRmjMnfydndu44GH6djeJFuF8sf4b)" style="width:100%; padding:10px 14px; background:rgba(0,0,0,0.6); border:1px solid var(--border-medium); border-radius:8px; color:#fff; font-family:'JetBrains Mono', monospace; font-size:13px; outline:none;" />
        <span style="font-size:11px; color:var(--text-tertiary);">Must be a valid TRON address linked to your FaucetPay account.</span>
      </div>

      <div style="display:flex; flex-direction:column; gap:6px;">
        <label style="font-size:12px; color:var(--text-secondary);">Email Confirmation Code (from Gmail inbox):</label>
        <div style="display:flex; gap:8px;">
          <input type="text" id="modal-wallet-code" placeholder="Enter 6-digit code..." style="flex:1; padding:10px 14px; background:rgba(0,0,0,0.6); border:1px solid var(--border-medium); border-radius:8px; color:#fff; font-family:'JetBrains Mono', monospace; font-size:13px; outline:none;" />
          <button class="btn-action" style="padding:8px 12px; font-size:12px; white-space:nowrap;" onclick="requestWalletCode()">📩 Send Code</button>
        </div>
        <span style="font-size:11px; color:var(--text-tertiary);">Click 'Send Code' to receive confirmation code on your Gmail, then enter it above.</span>
      </div>

      <div style="display:flex; justify-content:flex-end; gap:10px; margin-top:8px;">
        <button class="btn-action" onclick="closeWalletModal()" style="padding:8px 16px;">Cancel</button>
        <button class="btn-action" style="background:var(--emerald-bright); color:#000; font-weight:800; padding:8px 18px; border:none;" onclick="saveWalletFromModal()">Save & Sync Wallet</button>
      </div>
    </div>
  </div>

  <!-- TOAST CONTAINER -->
  <div class="toast-container" id="toast-container"></div>

  <script>
    /* STATE MANAGEMENT */
    let globalState = null;
    let selectedThreshold = 0.10;
    let accountFilter = 'all';
    let searchQuery = '';
    let viewMode = 'grid'; // 'grid' or 'matrix'
    let logChannel = 'all';
    let logLevelFilter = 'ALL';
    let autoScrollEnabled = true;
    let currentModalEmail = '';

    /* INITIALIZATION FUNCTION */
    function initDashboard() {
      // Load saved withdrawal threshold from localStorage
      try {
        const savedThreshold = localStorage.getItem('lw_withdraw_threshold');
        if (savedThreshold) {
          selectedThreshold = parseFloat(savedThreshold) || 0.10;
        }
      } catch (e) {}
      updateThresholdUI();

      // Load saved view mode
      try {
        const savedView = localStorage.getItem('lw_view_mode');
        if (savedView === 'grid' || savedView === 'matrix') {
          setViewMode(savedView);
        }
      } catch (e) {}

      fetchStats();
      setInterval(fetchStats, 2000);
    }

    // Live local second-by-second countdown decrement for smooth visual transitions
    setInterval(() => {
      if (!globalState || !globalState.accounts) return;
      let hasUpdate = false;
      globalState.accounts.forEach(acc => {
        if (acc.status === 'SLEEPING' && acc.countdown_sleep > 0) {
          acc.countdown_sleep -= 1;
          hasUpdate = true;
        }
      });
      if (hasUpdate && document.getElementById('accounts-grid')) {
        renderAccounts(globalState.accounts);
      }
    }, 1000);

    /* AUTO RUN */
    initDashboard();
    window.addEventListener('load', initDashboard);

    /* THRESHOLD LOGIC */
    function setThreshold(val) {
      selectedThreshold = parseFloat(val);
      localStorage.setItem('lw_withdraw_threshold', selectedThreshold.toString());
      document.getElementById('custom-threshold-input').value = '';
      updateThresholdUI();
      if (globalState) renderAll();
      showToast(`Withdrawal threshold set to $${selectedThreshold.toFixed(2)} USD`);
    }

    function handleCustomThreshold(val) {
      const num = parseFloat(val);
      if (num && num > 0) {
        selectedThreshold = num;
        localStorage.setItem('lw_withdraw_threshold', selectedThreshold.toString());
        updateThresholdUI();
        if (globalState) renderAll();
        showToast(`Custom threshold set to $${selectedThreshold.toFixed(2)} USD`);
      }
    }

    function updateThresholdUI() {
      const presets = [0.10, 0.50, 1.00, 5.00];
      presets.forEach(p => {
        const btn = document.getElementById(`pill-${p.toFixed(2).replace('.', '')}`);
        if (btn) {
          if (Math.abs(selectedThreshold - p) < 0.001) {
            btn.classList.add('active');
          } else {
            btn.classList.remove('active');
          }
        }
      });
      if (!presets.some(p => Math.abs(selectedThreshold - p) < 0.001)) {
        document.getElementById('custom-threshold-input').value = selectedThreshold;
      }
    }

    /* VIEW SWITCHER */
    function setViewMode(mode) {
      viewMode = mode;
      localStorage.setItem('lw_view_mode', mode);
      document.getElementById('view-btn-grid').classList.toggle('active', mode === 'grid');
      document.getElementById('view-btn-matrix').classList.toggle('active', mode === 'matrix');
      document.getElementById('accounts-grid').style.display = mode === 'grid' ? 'grid' : 'none';
      document.getElementById('accounts-matrix').style.display = mode === 'matrix' ? 'block' : 'none';
      if (globalState) renderAccounts(globalState.accounts || []);
    }

    /* FILTERING */
    function setAccountFilter(filter, el) {
      accountFilter = filter;
      document.querySelectorAll('.filter-tab-btn').forEach(b => b.classList.remove('active'));
      if (el) el.classList.add('active');
      if (globalState) renderAccounts(globalState.accounts || []);
    }

    function handleSearch(query) {
      searchQuery = query.toLowerCase().trim();
      if (globalState) renderAccounts(globalState.accounts || []);
    }

    /* DATA FETCHING */
    async function fetchStats() {
      try {
        const res = await fetch('/api/stats');
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        globalState = data;
        renderAll();
      } catch (err) {
        console.error('Telemetry fetch error:', err);
        document.getElementById('hud-sync-time').textContent = '⚠️ Reconnecting...';
      }
    }

    function renderAll() {
      if (!globalState) return;
      renderHUD(globalState);
      renderWithdrawalHub(globalState);
      renderAccounts(globalState.accounts || []);
      renderTerminalTabs(globalState.accounts || []);
      renderLogs();
    }

    /* HUD RENDERING */
    function renderHUD(data) {
      const s = data.summary || {};
      document.getElementById('hud-total-balance').textContent = '$' + (s.total_balance || '0.0000000') + ' USD';
      document.getElementById('hud-total-clovers').textContent = Number(s.total_clovers || 0).toLocaleString();
      document.getElementById('hud-total-tasks').textContent = Number(s.total_tasks_today || 0).toLocaleString() + ' videos';
      
      const activeW = s.active_workers || 0;
      const sleepW = s.sleeping_workers || 0;
      document.getElementById('hud-worker-status').innerHTML = `
        <span style="color: var(--emerald-bright);">${activeW} Active</span> / <span style="color: var(--amber-bright);">${sleepW} Sleep</span>
      `;
      
      document.getElementById('hud-total-accounts').textContent = `${s.total_accounts || 0} Configured Nodes`;
      document.getElementById('hud-sync-time').textContent = `Last sync: ${data.updated_at ? data.updated_at.split(' ')[1] : '-'}`;
      document.getElementById('service-status-text').textContent = data.service_status || 'FLEET RUNNING 24/7';
    }

    /* WITHDRAWAL HUB RENDERING */
    function renderWithdrawalHub(data) {
      // Update Auto-Withdraw Pill
      const autoBtn = document.getElementById('btn-toggle-auto-withdraw');
      if (autoBtn && data.auto_withdraw) {
        const isAuto = data.auto_withdraw.enabled;
        const autoThresh = data.auto_withdraw.threshold_usd || 0.10;
        autoBtn.textContent = isAuto ? `⚡ ON ($${autoThresh.toFixed(2)})` : '⚡ OFF';
        autoBtn.style.background = isAuto ? 'rgba(16, 185, 129, 0.2)' : 'rgba(255, 255, 255, 0.05)';
        autoBtn.style.color = isAuto ? '#34D399' : 'var(--text-tertiary)';
        autoBtn.style.border = isAuto ? '1px solid rgba(16, 185, 129, 0.4)' : '1px solid var(--border-subtle)';
      }

      const accounts = data.accounts || [];
      const totalBalance = parseFloat(data.summary?.total_balance || 0);
      const target = selectedThreshold;
      
      const globalPct = Math.min(100, Math.round((totalBalance / target) * 100));
      const fillEl = document.getElementById('global-progress-fill');
      fillEl.style.width = globalPct + '%';
      
      if (globalPct >= 100) {
        fillEl.classList.add('gold-tier');
      } else {
        fillEl.classList.remove('gold-tier');
      }

      document.getElementById('global-progress-pct').textContent = globalPct + '%';
      document.getElementById('global-current-num').textContent = `$${totalBalance.toFixed(7)} USD`;
      document.getElementById('global-target-num').textContent = `Target: $${target.toFixed(2)} USD`;

      const readyAccounts = accounts.filter(a => parseFloat(a.balance) >= target);
      document.getElementById('accounts-ready-count').textContent = `${readyAccounts.length} / ${accounts.length} Accounts`;

      const verdictEl = document.getElementById('readiness-verdict-box');
      if (readyAccounts.length > 0) {
        verdictEl.innerHTML = `
          <div class="status-gold-badge" style="background: rgba(234, 179, 8, 0.2); border-color: rgba(234, 179, 8, 0.6); color: #FDE047;">
            <svg style="width:14px; height:14px; stroke:currentColor; fill:none;" viewBox="0 0 24 24"><polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/></svg>
            <span>READY TO WITHDRAW (${readyAccounts.length})</span>
          </div>
        `;
      } else {
        verdictEl.innerHTML = `
          <div class="status-gold-badge" style="background: rgba(56, 189, 248, 0.1); border-color: rgba(56, 189, 248, 0.3); color: var(--cyan);">
            <div class="pulse-dot" style="background: var(--cyan);"></div>
            <span>ACCUMULATING FLEET</span>
          </div>
        `;
      }
    }

    /* ACCOUNTS FILTER & RENDERING */
    function renderAccounts(accounts) {
      // Update count badges
      document.getElementById('count-all').textContent = accounts.length;
      document.getElementById('count-active').textContent = accounts.filter(a => a.status === 'ACTIVE').length;
      document.getElementById('count-sleeping').textContent = accounts.filter(a => a.status === 'SLEEPING').length;
      document.getElementById('count-ready').textContent = accounts.filter(a => parseFloat(a.balance) >= selectedThreshold).length;
      document.getElementById('count-error').textContent = accounts.filter(a => a.status === 'ERROR').length;

      // Filter
      let filtered = accounts.filter(acc => {
        if (accountFilter === 'active' && acc.status !== 'ACTIVE') return false;
        if (accountFilter === 'sleeping' && acc.status !== 'SLEEPING') return false;
        if (accountFilter === 'error' && acc.status !== 'ERROR') return false;
        if (accountFilter === 'ready' && parseFloat(acc.balance) < selectedThreshold) return false;

        if (searchQuery) {
          const matchEmail = acc.email?.toLowerCase().includes(searchQuery) || acc.email_redacted?.toLowerCase().includes(searchQuery);
          const matchIp = acc.egress_ip?.toLowerCase().includes(searchQuery) || acc.country_name?.toLowerCase().includes(searchQuery);
          if (!matchEmail && !matchIp) return false;
        }
        return true;
      });

      if (viewMode === 'grid') {
        renderCardGrid(filtered);
      } else {
        renderMatrixTable(filtered);
      }
    }

    function renderCardGrid(accounts) {
      const grid = document.getElementById('accounts-grid');
      if (accounts.length === 0) {
        grid.innerHTML = `<div style="grid-column: 1/-1; text-align: center; color: var(--text-tertiary); padding: 40px;">No accounts match the current filter.</div>`;
        return;
      }

      grid.innerHTML = accounts.map((acc, idx) => {
        const bal = parseFloat(acc.balance);
        const isReady = bal >= selectedThreshold;
        const withdrawPct = Math.min(100, Math.round((bal / selectedThreshold) * 100));
        const dailyPct = Math.min(100, Math.round((acc.daily_done / (acc.daily_cap || 560)) * 100));
        const hourlyPct = Math.min(100, Math.round((acc.hourly_done / (acc.hourly_cap || 65)) * 100));

        let chipClass = 'chip-active';
        let statusText = acc.status;
        if (acc.status === 'SLEEPING') {
          chipClass = 'chip-sleeping';
          if (acc.countdown_sleep > 0) statusText = `SLEEP (${acc.countdown_sleep}s)`;
        } else if (acc.status === 'ERROR') {
          chipClass = 'chip-error';
        }

        const taskInfo = acc.current_task 
          ? `<span style="color: var(--cyan);">▶ [${acc.current_task.id}] ${acc.current_task.video_id} (${acc.current_task.duration}s)</span>`
          : `<span style="color: var(--text-tertiary);">Standby / Awaiting next cycle</span>`;

        const flag = acc.country === 'ID' ? '🇮🇩' : (acc.country === 'SG' ? '🇸🇬' : '🌐');
        const accPrefix = acc.email ? acc.email.split('@')[0] : `Node ${idx+1}`;

        return `
          <div class="account-card ${isReady ? 'ready-withdraw-border' : ''}">
            <div class="card-top-strip">
              <div class="acc-id-cluster">
                <div class="acc-avatar">${accPrefix.slice(0, 2).toUpperCase()}</div>
                <div class="acc-email-box">
                  <div class="email-label">
                    <span>${acc.email_redacted}</span>
                    <button class="copy-btn" onclick="copyToClipboard('${acc.email}')" title="Copy Full Email">
                      <svg viewBox="0 0 24 24"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
                    </button>
                  </div>
                  <div class="proxy-flag-chip">
                    <span>${flag} ${acc.country} • ${acc.egress_ip}</span>
                    <span style="font-size: 10px; margin-left: 4px; padding: 1px 5px; border-radius: 4px; ${acc.email_verified ? 'background: rgba(16,185,129,0.15); color: #34D399;' : 'background: rgba(245,158,11,0.15); color: #FBBF24;'}">
                      ${acc.email_verified ? '✓ Verified' : '⚠ Unverified'}
                    </span>
                    <span style="font-size: 10px; margin-left: 4px; padding: 1px 5px; border-radius: 4px; ${acc.daily_bonus_claimed ? 'background: rgba(16,185,129,0.15); color: #34D399;' : 'background: rgba(59,130,246,0.15); color: #60A5FA;'}" title="Daily Activity Bonus">
                      🎁 ${acc.daily_bonus_claimed ? '$0.01 Done' : (acc.daily_bonus_progress || '0/500')}
                    </span>
                  </div>
                </div>
              </div>
              <div class="status-badge-chip ${chipClass}">
                ${statusText}
              </div>
            </div>

            <div class="balances-row">
              <div class="balance-box">
                <div class="b-label">Live Balance</div>
                <div class="b-val mono tabular" style="color: var(--emerald-bright);">$${acc.balance}</div>
              </div>
              <div class="balance-box">
                <div class="b-label">Clovers</div>
                <div class="b-val mono tabular" style="color: var(--amber-bright);">${Number(acc.clovers).toLocaleString()}</div>
              </div>
            </div>

            <div class="card-quotas">
              <!-- WITHDRAWAL PROGRESS -->
              <div class="quota-item">
                <div class="quota-header">
                  <span>Withdraw Goal ($${selectedThreshold.toFixed(2)})</span>
                  <span class="mono tabular" style="color: ${isReady ? '#FDE047' : 'var(--emerald-bright)'}; font-weight: 700;">${withdrawPct}%</span>
                </div>
                <div class="mini-progress">
                  <div class="mini-progress-fill" style="width: ${withdrawPct}%; ${isReady ? 'background: var(--gold-gradient); box-shadow: 0 0 8px var(--gold-glow);' : ''}"></div>
                </div>
              </div>

              <!-- DAILY QUOTA -->
              <div class="quota-item">
                <div class="quota-header">
                  <span>Daily Quota: ${acc.daily_done}/${acc.daily_cap}</span>
                  <span class="mono tabular">${dailyPct}%</span>
                </div>
                <div class="mini-progress">
                  <div class="mini-progress-fill" style="width: ${dailyPct}%;"></div>
                </div>
              </div>

              <!-- HOURLY QUOTA -->
              <div class="quota-item">
                <div class="quota-header">
                  <span>Hourly Velocity: ${acc.hourly_done}/${acc.hourly_cap}</span>
                  <span class="mono tabular">${hourlyPct}%</span>
                </div>
                <div class="mini-progress">
                  <div class="mini-progress-fill" style="width: ${hourlyPct}%; background: linear-gradient(90deg, #F59E0B, #10B981);"></div>
                </div>
              </div>
            </div>

            <div class="activity-live-box">
              <div class="activity-content">${taskInfo}</div>
              <span class="mono" style="font-size: 10px; color: var(--text-tertiary);">${acc.last_activity_time}</span>
            </div>

            ${acc.last_payout ? `
              <div style="background: rgba(16, 185, 129, 0.08); border: 1px dashed rgba(16, 185, 129, 0.3); border-radius: 8px; padding: 6px 10px; display: flex; justify-content: space-between; align-items: center; font-size: 11px;">
                <div style="display: flex; align-items: center; gap: 6px;">
                  <span>🎉</span>
                  <span class="mono" style="font-weight: 700; color: var(--emerald-bright);">$${acc.last_payout.amount} USD</span>
                  <span class="status-badge-chip chip-active" style="padding: 2px 6px; font-size: 9.5px;">${acc.last_payout.status}</span>
                </div>
                <span class="mono" style="font-size: 10px; color: var(--text-tertiary);">${acc.last_payout.timestamp.split(' ')[1] || ''}</span>
              </div>
            ` : ''}

            <div class="card-footer-actions">
              <div style="display: flex; align-items: center; gap: 6px; flex: 1; min-width: 0;">
                <span class="mono" style="font-size: 10px; color: ${acc.server_wallet_set ? 'var(--emerald-bright)' : (acc.faucetpay_usdt_trc20 ? 'var(--cyan)' : 'var(--text-tertiary)')}; cursor: pointer; text-decoration: underline dotted;" onclick="openWalletModal('${acc.email}', '${acc.faucetpay_usdt_trc20 || ''}')" title="${acc.faucetpay_usdt_trc20 || 'Click to set FaucetPay USDT TRC20'}">
                  ${acc.server_wallet_set ? '🟢 TRC20: ' + acc.faucetpay_usdt_trc20.slice(0, 4) + '...' + acc.faucetpay_usdt_trc20.slice(-4) : (acc.faucetpay_usdt_trc20 ? '🟡 Local TRC20' : '⚠️ Set Wallet')}
                </span>
              </div>
              <div style="display: flex; gap: 6px;">
                ${!acc.email_verified ? `<button class="btn-action" style="padding: 3px 8px; font-size: 10.5px; background: rgba(245,158,11,0.15); color: #FBBF24; border: 1px solid rgba(245,158,11,0.3);" onclick="sendEmailVerify('${acc.email}')" title="Send email verification code">📩 Verif</button>` : ''}
                <button class="btn-action" style="padding: 3px 8px; font-size: 10.5px;" onclick="openWalletModal('${acc.email}', '${acc.faucetpay_usdt_trc20 || ''}')" title="Configure Wallet">⚙️</button>
                <button class="btn-action ${isReady && acc.faucetpay_usdt_trc20 ? 'gold-tier' : ''}" style="padding: 3px 8px; font-size: 10.5px; ${isReady && acc.faucetpay_usdt_trc20 ? 'background: var(--gold-gradient); color: #000; font-weight: 700;' : ''}" onclick="triggerWithdraw('${acc.email}')" ${!acc.faucetpay_usdt_trc20 || !isReady ? 'disabled title=\"Wallet not set or balance below threshold\"' : 'title=\"Withdraw to FaucetPay USDT TRC20\"'}>💸 Payout</button>
                <button class="btn-action" style="padding: 3px 8px; font-size: 10.5px;" onclick="focusAccountLogs('${acc.email_redacted}')">Logs</button>
              </div>
            </div>
          </div>
        `;
      }).join('');
    }

    function renderMatrixTable(accounts) {
      const tbody = document.getElementById('matrix-tbody');
      if (accounts.length === 0) {
        tbody.innerHTML = `<tr><td colspan="10" style="text-align: center; color: var(--text-tertiary); padding: 30px;">No accounts match the current filter.</td></tr>`;
        return;
      }

      tbody.innerHTML = accounts.map((acc, idx) => {
        const bal = parseFloat(acc.balance);
        const isReady = bal >= selectedThreshold;
        const withdrawPct = Math.min(100, Math.round((bal / selectedThreshold) * 100));
        const flag = acc.country === 'ID' ? '🇮🇩' : (acc.country === 'SG' ? '🇸🇬' : '🌐');

        let chipClass = 'chip-active';
        let statusText = acc.status;
        if (acc.status === 'SLEEPING') {
          chipClass = 'chip-sleeping';
          if (acc.countdown_sleep > 0) statusText = `SLEEP (${acc.countdown_sleep}s)`;
        } else if (acc.status === 'ERROR') {
          chipClass = 'chip-error';
        }

        const taskInfo = acc.current_task 
          ? `[${acc.current_task.id}] ${acc.current_task.video_id}`
          : `Idle`;

        return `
          <tr style="${isReady ? 'background: rgba(234, 179, 8, 0.04);' : ''}">
            <td>
              <div style="display: flex; align-items: center; gap: 8px;">
                <span class="mono" style="font-weight: 700;">${acc.email_redacted}</span>
                <button class="copy-btn" onclick="copyToClipboard('${acc.email}')">
                  <svg viewBox="0 0 24 24"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
                </button>
              </div>
            </td>
            <td><span class="status-badge-chip ${chipClass}">${statusText}</span></td>
            <td><span class="mono">${flag} ${acc.country} • ${acc.egress_ip}</span></td>
            <td><span class="mono tabular" style="color: var(--emerald-bright); font-weight: 700;">$${acc.balance}</span></td>
            <td><span class="mono tabular" style="color: var(--amber-bright); font-weight: 700;">${Number(acc.clovers).toLocaleString()}</span></td>
            <td>
              <div style="display: flex; align-items: center; gap: 6px;">
                <span class="mono" style="font-size: 11px; color: ${acc.faucetpay_usdt_trc20 ? 'var(--emerald-bright)' : 'var(--text-tertiary)'}; cursor: pointer; text-decoration: underline dotted;" onclick="openWalletModal('${acc.email}', '${acc.faucetpay_usdt_trc20 || ''}')">
                  ${acc.faucetpay_usdt_trc20 ? acc.faucetpay_usdt_trc20.slice(0, 4) + '...' + acc.faucetpay_usdt_trc20.slice(-4) : '⚠️ Set Wallet'}
                </span>
              </div>
            </td>
            <td>
              <div style="display: flex; align-items: center; gap: 8px; width: 100px;">
                <div class="mini-progress" style="flex: 1;">
                  <div class="mini-progress-fill" style="width: ${withdrawPct}%; ${isReady ? 'background: var(--gold-gradient);' : ''}"></div>
                </div>
                <span class="mono tabular" style="font-size: 11px; color: ${isReady ? '#FDE047' : 'var(--text-secondary)'};">${withdrawPct}%</span>
              </div>
            </td>
            <td><span class="mono tabular">${acc.daily_done} / ${acc.daily_cap}</span></td>
            <td><span class="mono tabular">${acc.hourly_done} / ${acc.hourly_cap}</span></td>
            <td><span class="mono" style="font-size: 11px; color: var(--cyan);">${taskInfo}</span></td>
            <td>
              <div style="display: flex; gap: 4px;">
                <button class="btn-action" style="padding: 2px 6px; font-size: 10px;" onclick="openWalletModal('${acc.email}', '${acc.faucetpay_usdt_trc20 || ''}')" title="Config Wallet">⚙️</button>
                <button class="btn-action ${isReady && acc.faucetpay_usdt_trc20 ? 'gold-tier' : ''}" style="padding: 2px 6px; font-size: 10px; ${isReady && acc.faucetpay_usdt_trc20 ? 'background: var(--gold-gradient); color: #000; font-weight: 700;' : ''}" onclick="triggerWithdraw('${acc.email}')" ${!acc.faucetpay_usdt_trc20 || !isReady ? 'disabled' : ''} title="Withdraw Payout">💸</button>
                <button class="btn-action" style="padding: 2px 6px; font-size: 10px;" onclick="focusAccountLogs('${acc.email_redacted}')">Stream</button>
              </div>
            </td>
          </tr>
        `;
      }).join('');
    }

    /* TERMINAL STREAM & TABS */
    function renderTerminalTabs(accounts) {
      const tabsContainer = document.getElementById('terminal-channel-tabs');
      const allActive = logChannel === 'all' ? 'active' : '';
      let html = `<button class="t-tab-btn ${allActive}" onclick="setLogChannel('all', this)">⚡ ALL FLEET LOGS</button>`;

      accounts.forEach(acc => {
        const isActive = logChannel === acc.email_redacted ? 'active' : '';
        const accPrefix = acc.email ? acc.email.split('@')[0] : acc.email_redacted;
        html += `<button class="t-tab-btn ${isActive}" onclick="setLogChannel('${acc.email_redacted}', this)">👤 ${accPrefix}</button>`;
      });

      tabsContainer.innerHTML = html;
    }

    function setLogChannel(channel, el) {
      logChannel = channel;
      document.querySelectorAll('.t-tab-btn').forEach(b => b.classList.remove('active'));
      if (el) el.classList.add('active');
      renderLogs();
    }

    function focusAccountLogs(redactedEmail) {
      logChannel = redactedEmail;
      renderTerminalTabs(globalState?.accounts || []);
      renderLogs();
      document.querySelector('.terminal-panel').scrollIntoView({ behavior: 'smooth' });
    }

    function handleLogLevelChange(level) {
      logLevelFilter = level;
      renderLogs();
    }

    function renderLogs() {
      if (!globalState) return;
      const windowEl = document.getElementById('terminal-stream-window');
      let lines = [];

      if (logChannel === 'all') {
        lines = globalState.logs || [];
      } else if (globalState.account_logs && globalState.account_logs[logChannel]) {
        lines = globalState.account_logs[logChannel];
      }

      if (lines.length === 0) {
        windowEl.innerHTML = `<div style="color: var(--text-tertiary); padding: 12px;">No active log lines for channel [${logChannel}].</div>`;
        return;
      }

      // Filter by Log Level
      let filteredLines = lines.filter(line => {
        if (logLevelFilter === 'SUCCESS') return line.includes('SUCCESS') || line.includes('REWARD') || line.includes('CLAIMED');
        if (logLevelFilter === 'WARN') return line.includes('WARNING') || line.includes('Quota') || line.includes('Sleeping');
        if (logLevelFilter === 'ERROR') return line.includes('ERROR') || line.includes('failed');
        return true;
      });

      windowEl.innerHTML = filteredLines.map(line => {
        let cls = 't-log-info';
        if (line.includes('SUCCESS') || line.includes('REWARD') || line.includes('REAL BALANCE') || line.includes('VALID')) {
          cls = 't-log-success';
        } else if (line.includes('WARNING') || line.includes('Sleeping') || line.includes('Batch') || line.includes('Streaming')) {
          cls = 't-log-warn';
        } else if (line.includes('ERROR') || line.includes('failed') || line.includes('checkpoint')) {
          cls = 't-log-error';
        }
        return `<div class="t-log-entry ${cls}">${escapeHtml(line)}</div>`;
      }).join('');

      if (autoScrollEnabled) {
        windowEl.scrollTop = windowEl.scrollHeight;
      }
    }

    function toggleAutoScroll() {
      autoScrollEnabled = !autoScrollEnabled;
      const btn = document.getElementById('btn-autoscroll');
      btn.textContent = autoScrollEnabled ? '🔒 Auto-Scroll: ON' : '🔓 Auto-Scroll: OFF';
      if (autoScrollEnabled) {
        const windowEl = document.getElementById('terminal-stream-window');
        windowEl.scrollTop = windowEl.scrollHeight;
      }
    }

    function clearTerminal() {
      document.getElementById('terminal-stream-window').innerHTML = `<div style="color: var(--text-tertiary); padding: 12px;">Terminal buffer cleared.</div>`;
    }

    function exportLogs() {
      if (!globalState || !globalState.logs) return;
      const logContent = globalState.logs.join(String.fromCharCode(10));
      const blob = new Blob([logContent], { type: 'text/plain;charset=utf-8' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `luckywatch-fleet-${new Date().toISOString().replace(/:/g, '-')}.log`;
      a.click();
      URL.revokeObjectURL(url);
      showToast('Logs exported successfully');
    }

    /* ACTIONS & TOASTS */
    async function triggerAction(actionName) {
      try {
        const res = await fetch(`/api/actions/${actionName}`, { method: 'POST' });
        const result = await res.json();
        showToast(result.message || `Action ${actionName} executed`);
        fetchStats();
      } catch (err) {
        showToast(`Action failed: ${err.message}`);
      }
    }

    function copyToClipboard(text) {
      if (!text) return;
      navigator.clipboard.writeText(text).then(() => {
        showToast(`Copied to clipboard: ${text}`);
      }).catch(() => {
        // Fallback
        const el = document.createElement('textarea');
        el.value = text;
        document.body.appendChild(el);
        el.select();
        document.execCommand('copy');
        document.body.removeChild(el);
        showToast(`Copied: ${text}`);
      });
    }

    async function sendEmailVerify(email) {
      showToast(`Sending verification link for ${email}...`);
      try {
        const res = await fetch('/api/actions/send_verify_email', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ email: email })
        });
        const result = await res.json();
        showToast(result.message || 'Verification email sent! Check inbox/spam.');
      } catch (err) {
        showToast(`Error sending verification email: ${err.message}`);
      }
    }

    async function toggleAutoWithdrawConfig() {
      if (!globalState || !globalState.auto_withdraw) return;
      const currentEnabled = globalState.auto_withdraw.enabled;
      const newEnabled = !currentEnabled;
      const threshold = selectedThreshold >= 0.10 ? selectedThreshold : 0.10;
      
      showToast(`Setting Auto-Withdraw to ${newEnabled ? 'ENABLED' : 'DISABLED'} (Threshold: $${threshold.toFixed(2)})...`);
      try {
        const res = await fetch('/api/actions/config_auto_withdraw', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ enabled: newEnabled, threshold_usd: threshold })
        });
        const result = await res.json();
        showToast(result.message || 'Auto-Withdraw updated!');
        fetchStats();
      } catch (err) {
        showToast(`Failed: ${err.message}`);
      }
    }
    function openWalletModal(email, currentWallet) {
      currentModalEmail = email;
      document.getElementById('modal-wallet-email').textContent = email;
      document.getElementById('modal-wallet-address').value = currentWallet || '';
      document.getElementById('modal-wallet-code').value = '';
      const modal = document.getElementById('wallet-modal');
      modal.style.display = 'flex';
      document.getElementById('modal-wallet-address').focus();
    }

    function closeWalletModal() {
      document.getElementById('wallet-modal').style.display = 'none';
      currentModalEmail = '';
    }

    async function requestWalletCode() {
      const wallet = document.getElementById('modal-wallet-address').value.trim();
      if (!wallet) {
        showToast('Please enter wallet address first');
        return;
      }
      showToast(`Requesting confirmation code for ${currentModalEmail}...`);
      try {
        const res = await fetch('/api/actions/save_wallet', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ email: currentModalEmail, wallet: wallet, code: '' })
        });
        const result = await res.json();
        showToast('Code sent to Gmail inbox! Please enter code.');
      } catch (err) {
        showToast(`Request code error: ${err.message}`);
      }
    }

    async function saveWalletFromModal() {
      const wallet = document.getElementById('modal-wallet-address').value.trim();
      const code = document.getElementById('modal-wallet-code').value.trim();
      if (!wallet) {
        showToast('Please enter a valid wallet address');
        return;
      }
      if (!wallet.startsWith('T') || wallet.length < 30) {
        showToast('Warning: TRON USDT TRC20 address should start with T...');
      }
      try {
        const res = await fetch('/api/actions/save_wallet', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ email: currentModalEmail, wallet: wallet, code: code })
        });
        const result = await res.json();
        showToast(result.message || 'Wallet saved successfully');
        if (result.status === 'ok') {
          closeWalletModal();
          fetchStats();
        }
      } catch (err) {
        showToast(`Save wallet failed: ${err.message}`);
      }
    }

    async function triggerWithdraw(email) {
      const acc = (globalState?.accounts || []).find(a => a.email === email);
      if (!acc) return;
      if (!acc.faucetpay_usdt_trc20) {
        openWalletModal(email, '');
        showToast('Please configure FaucetPay USDT TRC20 address first');
        return;
      }
      
      showToast(`Initiating payout request for ${acc.email_redacted}...`);

      try {
        const res = await fetch('/api/actions/withdraw', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ email: email })
        });
        const result = await res.json();
        showToast(result.message || 'Withdrawal request sent');
        fetchStats();
      } catch (err) {
        showToast(`Withdrawal error: ${err.message}`);
      }
    }

    async function triggerWithdrawAll() {
      const readyAccounts = (globalState?.accounts || []).filter(a => parseFloat(a.balance) >= selectedThreshold && a.faucetpay_usdt_trc20);
      if (readyAccounts.length === 0) {
        showToast('No accounts currently meet the threshold with configured wallet');
        return;
      }
      
      showToast(`Sending batch withdrawal request for ${readyAccounts.length} account(s)...`);

      try {
        const res = await fetch('/api/actions/withdraw_all', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ threshold: selectedThreshold })
        });
        const result = await res.json();
        showToast(result.message || 'Batch withdrawal request processed');
        fetchStats();
      } catch (err) {
        showToast(`Batch withdrawal error: ${err.message}`);
      }
    }

    function showToast(msg) {
      const container = document.getElementById('toast-container');
      const toast = document.createElement('div');
      toast.className = 'toast-msg';
      toast.innerHTML = `
        <svg style="width:14px; height:14px; stroke:var(--emerald-bright); fill:none;" viewBox="0 0 24 24"><polyline points="20 6 9 17 4 12"/></svg>
        <span>${escapeHtml(msg)}</span>
      `;
      container.appendChild(toast);
      setTimeout(() => {
        toast.style.opacity = '0';
        toast.style.transform = 'translateY(10px)';
        toast.style.transition = 'all 0.3s';
        setTimeout(() => toast.remove(), 300);
      }, 3000);
    }

    function escapeHtml(s) {
      if (!s) return '';
      return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    }
  </script>
</body>
</html>"""


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class Handler(BaseHTTPRequestHandler):
    def _send_json(self, status_code: int, data: Any):
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode("utf-8"))

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/" or path == "/index.html":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(DASHBOARD_HTML.encode("utf-8"))
        elif path == "/api/stats":
            stats = get_latest_stats()
            self._send_json(200, stats)
        else:
            self._send_json(404, {"error": "Not Found", "path": path})

    def do_POST(self):
        global _LAST_USER_FETCH, _GEO_CACHE
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/api/actions/retry":
            # Restart bot worker daemon via systemctl or direct signal
            try:
                subprocess.Popen(["systemctl", "restart", "luckywatch-bot.service"])
                self._send_json(200, {"status": "ok", "message": "Bot service restart triggered successfully."})
            except Exception as e:
                self._send_json(500, {"status": "error", "message": f"Failed to trigger retry: {e}"})

        elif path == "/api/actions/config_auto_withdraw":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length).decode("utf-8")) if length > 0 else {}
                
                if not CONFIG_FILE.exists():
                    self._send_json(500, {"status": "error", "message": "Config missing."})
                    return

                cfg = json.loads(CONFIG_FILE.read_text())
                auto_cfg = cfg.setdefault("auto_withdraw", {})
                if "enabled" in body:
                    auto_cfg["enabled"] = bool(body["enabled"])
                if "threshold_usd" in body:
                    auto_cfg["threshold_usd"] = float(body["threshold_usd"])
                if "service" not in auto_cfg:
                    auto_cfg["service"] = "faucetpayusdt"

                CONFIG_FILE.write_text(json.dumps(cfg, indent=2))
                self._send_json(200, {"status": "ok", "message": f"Auto-withdraw set to {'ON' if auto_cfg['enabled'] else 'OFF'} at ${auto_cfg['threshold_usd']:.2f} USD."})
            except Exception as e:
                self._send_json(500, {"status": "error", "message": f"Failed to update auto_withdraw config: {e}"})

        elif path == "/api/actions/save_wallet":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                target_email = body.get("email")
                wallet_addr = body.get("wallet", "").strip()
                confirm_code = body.get("code", "").strip()

                if not target_email or not wallet_addr:
                    self._send_json(400, {"status": "error", "message": "Email and wallet are required."})
                    return

                # Update config.json
                if CONFIG_FILE.exists():
                    cfg = json.loads(CONFIG_FILE.read_text())
                    found = False
                    for acc in cfg.get("accounts", []):
                        if acc.get("email") == target_email:
                            acc["faucetpay_usdt_trc20"] = wallet_addr
                            found = True
                            break
                    if found:
                        CONFIG_FILE.write_text(json.dumps(cfg, indent=2))

                # Update remote settings on LuckyWatch if session active
                remote_msg = "Saved locally in config."
                if STATE_FILE.exists():
                    try:
                        st = json.loads(STATE_FILE.read_text())
                        sess = st.get("sessions", {}).get(target_email, {})
                        cookie_str = sess.get("cookie_string", "")
                        if cookie_str:
                            proxy_url = next((a.get("proxy") for a in cfg.get("accounts", []) if a.get("email") == target_email), "http://127.0.0.1:31001")
                            opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url}))
                            req_save = urllib.request.Request(
                                "https://luckywatch.pro/api/user/settings/save/",
                                data=urllib.parse.urlencode({"method": "payments", "faucetpayusdt_wallet": wallet_addr, "code": confirm_code}).encode("utf-8"),
                                headers={"Cookie": cookie_str, "Content-Type": "application/x-www-form-urlencoded", "User-Agent": "Mozilla/5.0"},
                            )
                            s_res = json.loads(opener.open(req_save, timeout=6).read().decode("utf-8"))
                            if s_res.get("status") == "ok":
                                if "time" in s_res.get("data", {}):
                                    remote_msg = "Confirmation code sent to Gmail! Please input code to complete sync."
                                else:
                                    remote_msg = "Successfully synced to LuckyWatch server!"
                            else:
                                remote_msg = f"Server response: {s_res.get('message')}"
                    except Exception as e:
                        remote_msg = f"Remote sync notice: {e}"

                # Clear cache
                _LAST_USER_FETCH.clear()
                self._send_json(200, {"status": "ok", "message": remote_msg})
            except Exception as e:
                self._send_json(500, {"status": "error", "message": f"Failed to save wallet: {e}"})

        elif path == "/api/actions/send_verify_email":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length).decode("utf-8")) if length > 0 else {}
                target_email = body.get("email")

                if not target_email or not STATE_FILE.exists() or not CONFIG_FILE.exists():
                    self._send_json(400, {"status": "error", "message": "Invalid request."})
                    return

                cfg = json.loads(CONFIG_FILE.read_text())
                st = json.loads(STATE_FILE.read_text())
                sess = st.get("sessions", {}).get(target_email, {})
                cookie_str = sess.get("cookie_string", "")

                if not cookie_str:
                    self._send_json(400, {"status": "error", "message": "No active session for this account."})
                    return

                proxy_url = next((a.get("proxy") for a in cfg.get("accounts", []) if a.get("email") == target_email), "http://127.0.0.1:31001")
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url}))
                req = urllib.request.Request(
                    "https://luckywatch.pro/api/user/settings/confirm/",
                    data=urllib.parse.urlencode({"method": "reqConfirm"}).encode("utf-8"),
                    headers={"Cookie": cookie_str, "Content-Type": "application/x-www-form-urlencoded", "User-Agent": "Mozilla/5.0"},
                )
                res = json.loads(opener.open(req, timeout=6).read().decode("utf-8"))
                if res.get("status") == "ok":
                    self._send_json(200, {"status": "ok", "message": f"Verification email sent to {target_email}! Please check inbox & click confirm link."})
                else:
                    self._send_json(200, {"status": "notice", "message": f"Server response: {res.get('message')}."})
            except Exception as e:
                self._send_json(500, {"status": "error", "message": f"Failed to send verify email: {e}"})

        elif path == "/api/actions/withdraw":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                target_email = body.get("email")

                if not CONFIG_FILE.exists() or not STATE_FILE.exists():
                    self._send_json(500, {"status": "error", "message": "Config or session state missing."})
                    return

                cfg = json.loads(CONFIG_FILE.read_text())
                st = json.loads(STATE_FILE.read_text())

                acc = next((a for a in cfg.get("accounts", []) if a.get("email") == target_email), None)
                if not acc:
                    self._send_json(404, {"status": "error", "message": f"Account {target_email} not found."})
                    return

                wallet = acc.get("faucetpay_usdt_trc20", "").strip()
                if not wallet:
                    self._send_json(400, {"status": "error", "message": "FaucetPay USDT TRC20 wallet is not set."})
                    return

                sess = st.get("sessions", {}).get(target_email, {})
                cookie_str = sess.get("cookie_string", "")
                if not cookie_str:
                    self._send_json(400, {"status": "error", "message": "No active session cookie."})
                    return

                proxy_url = acc.get("proxy", "http://127.0.0.1:31001")
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url}))

                # Fetch balance
                req_u = urllib.request.Request(
                    "https://luckywatch.pro/api/user/",
                    data=urllib.parse.urlencode({"method": "getCurrentUser"}).encode("utf-8"),
                    headers={"Cookie": cookie_str, "Content-Type": "application/x-www-form-urlencoded", "User-Agent": "Mozilla/5.0"},
                )
                u_res = json.loads(opener.open(req_u, timeout=5).read().decode("utf-8"))
                bal = float(u_res.get("data", {}).get("balance", 0.0))

                if bal < 0.10:
                    self._send_json(400, {"status": "error", "message": f"Balance (${bal:.4f}) is below minimum $0.10 USD."})
                    return

                # Execute payout send API
                req_p = urllib.request.Request(
                    "https://luckywatch.pro/api/user/payout/send/",
                    data=urllib.parse.urlencode({"sum": f"{bal:.7f}", "service": "faucetpayusdt", "captcha": ""}).encode("utf-8"),
                    headers={"Cookie": cookie_str, "Content-Type": "application/x-www-form-urlencoded", "User-Agent": "Mozilla/5.0"},
                )
                p_res = json.loads(opener.open(req_p, timeout=8).read().decode("utf-8"))
                _LAST_USER_FETCH.clear()

                if p_res.get("status") == "ok":
                    self._send_json(200, {"status": "ok", "message": f"🎉 Withdrawal of ${bal:.4f} USD sent to {wallet[:6]}...{wallet[-4:]}!"})
                else:
                    self._send_json(200, {"status": "notice", "message": f"Payout response: {p_res.get('message', 'Processed')}."})
            except Exception as e:
                self._send_json(500, {"status": "error", "message": f"Withdrawal request error: {e}"})

        elif path == "/api/actions/withdraw_all":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length).decode("utf-8")) if length > 0 else {}
                thresh = float(body.get("threshold", 0.10))

                cfg = json.loads(CONFIG_FILE.read_text()) if CONFIG_FILE.exists() else {}
                st = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}

                successes = []
                failures = []

                for acc in cfg.get("accounts", []):
                    email = acc.get("email")
                    wallet = acc.get("faucetpay_usdt_trc20", "").strip()
                    if not wallet:
                        continue

                    sess = st.get("sessions", {}).get(email, {})
                    cookie_str = sess.get("cookie_string", "")
                    if not cookie_str:
                        continue

                    proxy_url = acc.get("proxy", "http://127.0.0.1:31001")
                    try:
                        opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url}))
                        req_u = urllib.request.Request(
                            "https://luckywatch.pro/api/user/",
                            data=urllib.parse.urlencode({"method": "getCurrentUser"}).encode("utf-8"),
                            headers={"Cookie": cookie_str, "Content-Type": "application/x-www-form-urlencoded", "User-Agent": "Mozilla/5.0"},
                        )
                        u_res = json.loads(opener.open(req_u, timeout=4).read().decode("utf-8"))
                        bal = float(u_res.get("data", {}).get("balance", 0.0))

                        if bal >= thresh and bal >= 0.10:
                            req_p = urllib.request.Request(
                                "https://luckywatch.pro/api/user/payout/send/",
                                data=urllib.parse.urlencode({"sum": f"{bal:.7f}", "service": "faucetpayusdt", "captcha": ""}).encode("utf-8"),
                                headers={"Cookie": cookie_str, "Content-Type": "application/x-www-form-urlencoded", "User-Agent": "Mozilla/5.0"},
                            )
                            p_res = json.loads(opener.open(req_p, timeout=8).read().decode("utf-8"))
                            successes.append(f"{email.split('@')[0]} (${bal:.4f})")
                    except Exception as e:
                        failures.append(f"{email.split('@')[0]}: {e}")

                _LAST_USER_FETCH.clear()
                msg = f"Auto-Withdraw executed. Success: {len(successes)} accounts ({', '.join(successes) if successes else 'None'})."
                if failures:
                    msg += f" Notice: {len(failures)} failed."
                self._send_json(200, {"status": "ok", "message": msg})
            except Exception as e:
                self._send_json(500, {"status": "error", "message": f"Batch withdraw failed: {e}"})

        elif path == "/api/actions/refresh":
            # Clear in-memory caches to force instant fresh fetch on next poll
            _LAST_USER_FETCH.clear()
            _GEO_CACHE.clear()
            self._send_json(200, {"status": "ok", "message": "Session and telemetry caches cleared."})

        else:
            self._send_json(404, {"error": "Endpoint not found", "path": path})

    def log_message(self, format, *args):
        pass


def run():
    server = ThreadingHTTPServer(("127.0.0.1", 8280), Handler)
    logger.info("LuckyWatch Multi-Account Dashboard Server running on http://127.0.0.1:8280")
    server.serve_forever()


if __name__ == "__main__":
    run()
