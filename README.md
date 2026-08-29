# 🍀 LuckyWatch Auto-Watch & Claim Bot

<p align="center">
  <strong>Enterprise Pure Python HTTP Stream Automator, Turnstile Bypass & Rewards Engine</strong>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.11%2B-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python 3.11+" />
  <img src="https://img.shields.io/badge/Driver-100%25%20Pure%20HTTP%20REST-green?style=for-the-badge" alt="Pure HTTP" />
  <img src="https://img.shields.io/badge/Turnstile-Auto--Solver%20Bypass-F38020?style=for-the-badge&logo=cloudflare&logoColor=white" alt="Turnstile Bypass" />
  <img src="https://img.shields.io/badge/Proxy-Surfshark%20Egress%20Ready-2496ED?style=for-the-badge" alt="Proxy Ready" />
</p>

---

## 🌟 Overview

**LuckyWatch Bot** is a high-performance, lightweight automation engine designed to watch videos, solve security challenges, and claim daily task rewards on the LuckyWatch platform completely via **pure Python HTTP requests** (Zero Playwright / Zero Selenium / Zero Browser overhead).

---

## 🚀 Key Features

* **100% Pure Python HTTP REST Client:** Zero headless browser bloat, sub-second execution times, and ultra-low RAM footprint (< 25MB).
* **Automated Cloudflare Turnstile Bypass:** Seamlessly connects to the local Turnstile Solver API to obtain valid challenge tokens on demand.
* **Persistent Session Cache:** Caches session tokens (`hash`, `signed`) in `state/sessions.json` so re-authentication happens only when sessions expire.
* **Smart Video Watching Loop:**
  * Auto-fetches next task from `/api/user/tasks/`.
  * Signals task initiation (`fin: 0`).
  * Simulates precise video playback stream duration.
  * Claims reward credit (`fin: 1`).
* **Daily Bonus Auto-Claimer:** Automatically collects daily streak bonuses.
* **Surfshark Proxy Egress Isolation:** Routes all traffic through local proxy nodes to prevent rate limiting or geo-blocking.

---

## 🏗️ Project Structure

```text
luckywatch-bot/
├── bot.py             # Main CLI & Automated Watch Loop Engine
├── config.json        # User accounts, proxy settings & runner thresholds
├── state/             # Persistent session store (auto-created)
│   └── sessions.json  # Cached authentication cookies
└── README.md          # Architecture & documentation
```

---

## ⚡ Quickstart

### 1. Configure Credentials (`config.json`)
```json
{
  "accounts": [
    {
      "email": "your_email@example.com",
      "password": "your_password",
      "active": true
    }
  ],
  "proxy": {
    "enabled": true,
    "url": "http://127.0.0.1:31001",
    "health_check_url": "https://api.ipify.org?format=json"
  },
  "runner": {
    "max_videos_per_cycle": 50,
    "delay_between_videos_seconds": 2,
    "auto_daily_bonus": true
  }
}
```

### 2. Run the Bot
```bash
python3 bot.py
```

---

## 🛡️ License
Private project. All rights reserved.
