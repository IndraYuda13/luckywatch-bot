#!/usr/bin/env python3
"""
LuckyWatch Live Web Dashboard & Telemetry Server (Pure Python Stdlib HTTP Server)
---------------------------------------------------------------------------------
Zero external dependencies (uses standard library http.server).
Port: 8280 -> Cloudflare Tunnel: https://luckywatch.indrayuda.my.id
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

LOG_FILE = Path("/root/projects/luckywatch-bot/bot.log")
STATE_FILE = Path("/root/projects/luckywatch-bot/state/sessions.json")


def get_latest_stats() -> dict:
    balance = "0.0000000"
    clovers = "0"
    email = "Multiple Accounts"
    proxy = "Multi-Node Setup"
    accounts_info = []

    # Always fetch live real balance directly from server state
    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text())
            sessions = state.get("sessions", {})
            for acc_email, sess in sessions.items():
                cookie_str = sess.get("cookie_string", "")
                if cookie_str:
                    try:
                        opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": "http://127.0.0.1:31001", "https": "http://127.0.0.1:31001"}))
                        req = urllib.request.Request(
                            "https://luckywatch.pro/api/user/",
                            data=urllib.parse.urlencode({"method": "getCurrentUser"}).encode("utf-8"),
                            headers={
                                "User-Agent": "Mozilla/5.0 (Linux; Android 14; Pixel 8 Pro) AppleWebKit/537.36",
                                "Cookie": cookie_str,
                                "Content-Type": "application/x-www-form-urlencoded",
                            },
                        )
                        res = json.loads(opener.open(req, timeout=5).read())
                        if res.get("status") == "ok" and "balance" in res.get("data", {}):
                            accounts_info.append({
                                "email": acc_email,
                                "balance": str(res["data"]["balance"]),
                                "clovers": str(res["data"]["clover"])
                            })
                    except Exception:
                        pass
        except Exception:
            pass

    # Extract log lines
    log_lines = []
    if LOG_FILE.exists():
        try:
            lines = LOG_FILE.read_text().strip().splitlines()
            log_lines = lines[-60:]
        except Exception:
            pass

    # Calculate total balance
    total_bal = sum([float(a["balance"]) for a in accounts_info]) if accounts_info else 0.0
    total_clv = sum([int(a["clovers"]) for a in accounts_info]) if accounts_info else 0

    return {
        "email": f"{len(accounts_info)} Active Account(s)" if accounts_info else "Multi-Worker",
        "balance": f"{total_bal:.7f}",
        "clovers": str(total_clv),
        "accounts": accounts_info,
        "proxy": "Node 01 (ID) + Node 02 (SG)",
        "logs": log_lines,
        "service_status": "RUNNING 24/7 (MULTI-THREAD)",
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>LuckyWatch Live Telemetry</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">
  <style>
    :root {
      color-scheme: dark;
      --bg: #0B0F17;
      --panel: rgba(17, 24, 39, 0.85);
      --card: rgba(21, 30, 49, 0.75);
      --border: rgba(255, 255, 255, 0.09);
      --accent-green: #10B981;
      --accent-cyan: #38BDF8;
      --accent-yellow: #F59E0B;
      --text: #F8FAFC;
      --text-muted: #94A3B8;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      background: var(--bg);
      color: var(--text);
      font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
      min-height: 100vh;
      padding: 24px 16px;
      display: flex;
      flex-direction: column;
      align-items: center;
    }
    .container {
      width: 100%;
      max-width: 1100px;
      display: flex;
      flex-direction: column;
      gap: 20px;
    }
    .header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      background: var(--panel);
      backdrop-filter: blur(20px);
      border: 1px solid var(--border);
      border-radius: 16px;
      padding: 18px 24px;
      box-shadow: 0 8px 32px rgba(0,0,0,0.4);
    }
    .brand {
      display: flex;
      align-items: center;
      gap: 12px;
    }
    .brand-icon {
      width: 40px;
      height: 40px;
      background: linear-gradient(135deg, #10B981, #059669);
      border-radius: 10px;
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 22px;
    }
    .brand h1 {
      font-size: 18px;
      font-weight: 800;
      letter-spacing: -0.5px;
    }
    .status-badge {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      background: rgba(16, 185, 129, 0.12);
      border: 1px solid rgba(16, 185, 129, 0.3);
      color: #34D399;
      padding: 8px 16px;
      border-radius: 9999px;
      font-size: 12px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.5px;
    }
    .pulse {
      width: 8px;
      height: 8px;
      background: #10B981;
      border-radius: 50%;
      box-shadow: 0 0 10px #10B981;
      animation: pulse 1.5s infinite;
    }
    @keyframes pulse {
      0% { transform: scale(0.95); box-shadow: 0 0 0 0 rgba(16, 185, 129, 0.7); }
      70% { transform: scale(1); box-shadow: 0 0 0 8px rgba(16, 185, 129, 0); }
      100% { transform: scale(0.95); box-shadow: 0 0 0 0 rgba(16, 185, 129, 0); }
    }
    .grid-kpi {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
      gap: 16px;
    }
    .card {
      background: var(--card);
      backdrop-filter: blur(16px);
      border: 1px solid var(--border);
      border-radius: 16px;
      padding: 20px;
      display: flex;
      flex-direction: column;
      gap: 6px;
      box-shadow: 0 4px 20px rgba(0,0,0,0.25);
    }
    .card-label {
      font-size: 12px;
      font-weight: 600;
      color: var(--text-muted);
      text-transform: uppercase;
      letter-spacing: 0.5px;
    }
    .card-val {
      font-size: 26px;
      font-weight: 800;
      font-family: 'JetBrains Mono', monospace;
      color: var(--text);
    }
    .card-sub {
      font-size: 12px;
      color: var(--text-muted);
    }
    .log-panel {
      background: #070A10;
      border: 1px solid var(--border);
      border-radius: 16px;
      padding: 20px;
      display: flex;
      flex-direction: column;
      gap: 12px;
    }
    .log-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
    }
    .log-header h2 {
      font-size: 13px;
      font-weight: 700;
      color: var(--text-muted);
      text-transform: uppercase;
      letter-spacing: 0.5px;
    }
    .log-stream {
      background: rgba(0,0,0,0.7);
      border: 1px solid rgba(255,255,255,0.05);
      border-radius: 10px;
      padding: 16px;
      height: 400px;
      overflow-y: auto;
      font-family: 'JetBrains Mono', monospace;
      font-size: 12px;
      line-height: 1.6;
      color: #E2E8F0;
      white-space: pre-wrap;
    }
    .log-line {
      margin-bottom: 4px;
      padding: 2px 0;
      border-bottom: 1px solid rgba(255,255,255,0.02);
    }
    .log-success { color: #34D399; }
    .log-warn { color: #FBBF24; }
    .log-err { color: #F87171; }
    .log-info { color: #94A3B8; }
  </style>
</head>
<body>
  <div class="container">
    <div class="header">
      <div class="brand">
        <div class="brand-icon">🍀</div>
        <div>
          <h1>LuckyWatch Auto-Watch Live Stream</h1>
          <p style="font-size: 12px; color: var(--text-muted);">24/7 Pure Python HTTP Automation • Jakarta Egress Node</p>
        </div>
      </div>
      <div class="status-badge">
        <div class="pulse"></div>
        <span id="service-status">ACTIVE 24/7 DAEMON</span>
      </div>
    </div>

    <div class="grid-kpi">
      <div class="card">
        <div class="card-label">Account Balance</div>
        <div class="card-val" id="val-balance" style="color: #34D399;">$...</div>
        <div class="card-sub" id="val-email">user@example.com</div>
      </div>
      <div class="card">
        <div class="card-label">Clover Coins</div>
        <div class="card-val" id="val-clovers" style="color: #FBBF24;">...</div>
        <div class="card-sub">Rewards Multiplier Tier</div>
      </div>
      <div class="card">
        <div class="card-label">Egress Proxy Node</div>
        <div class="card-val" style="font-size: 18px; color: #38BDF8;">Node 01 (ID)</div>
        <div class="card-sub">93.185.162.118 (Jakarta)</div>
      </div>
      <div class="card">
        <div class="card-label">Last Ping / Sync</div>
        <div class="card-val" id="val-time" style="font-size: 16px;">...</div>
        <div class="card-sub">Auto-refreshes every 2s</div>
      </div>
    </div>

    <div class="log-panel">
      <div class="log-header">
        <h2>Live Execution Stream (Tail Log)</h2>
        <span style="font-size: 12px; color: var(--accent-green); font-family: monospace;">● LIVE STREAM</span>
      </div>
      <div class="log-stream" id="log-stream">Connecting to live bot stream...</div>
    </div>
  </div>

  <script>
    async function fetchStats() {
      try {
        const res = await fetch('/api/stats');
        const data = await res.json();
        
        document.getElementById('val-balance').textContent = '$' + data.balance + ' USD';
        document.getElementById('val-clovers').textContent = data.clovers;
        document.getElementById('val-email').textContent = data.email;
        document.getElementById('val-time').textContent = data.updated_at;
        
        const stream = document.getElementById('log-stream');
        stream.innerHTML = data.logs.map(line => {
          let cls = 'log-info';
          if (line.includes('SUCCESS') || line.includes('VALID') || line.includes('ACTIVE') || line.includes('REAL BALANCE')) cls = 'log-success';
          else if (line.includes('WARNING') || line.includes('Quota') || line.includes('Batch') || line.includes('Streaming')) cls = 'log-warn';
          else if (line.includes('ERROR') || line.includes('failed')) cls = 'log-err';
          return `<div class="log-line ${cls}">${escapeHtml(line)}</div>`;
        }).join('');
        
        stream.scrollTop = stream.scrollHeight;
      } catch (e) {
        console.error('Sync error:', e);
      }
    }

    function escapeHtml(s) {
      return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    }

    setInterval(fetchStats, 2000);
    fetchStats();
  </script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/" or self.path.startswith("/?"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(DASHBOARD_HTML.encode("utf-8"))
        elif self.path.startswith("/api/stats"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            payload = json.dumps(get_latest_stats()).encode("utf-8")
            self.wfile.write(payload)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass


def run():
    server = HTTPServer(("127.0.0.1", 8280), Handler)
    print("LuckyWatch Dashboard Server running on http://127.0.0.1:8280")
    server.serve_forever()


if __name__ == "__main__":
    run()
