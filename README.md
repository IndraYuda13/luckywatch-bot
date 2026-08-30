# 🍀 LuckyWatch Fleet Engine & Telemetry System

<p align="center">
  <strong>Enterprise Multi-Account Pure Python HTTP Stream Automator, AI Vision Solver & Real-Time Telemetry Dashboard</strong>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.11%2B-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python 3.11+" />
  <img src="https://img.shields.io/badge/Engine-100%25%20Pure%20HTTP%20REST%20(Zero%20Browser)-success?style=for-the-badge" alt="Pure HTTP" />
  <img src="https://img.shields.io/badge/Turnstile-Rotational%20Node%20Bypass-F38020?style=for-the-badge&logo=cloudflare&logoColor=white" alt="Turnstile Bypass" />
  <img src="https://img.shields.io/badge/AI%20Vision-IconCaptcha%20Solver%20(Port%205073)-7952B3?style=for-the-badge" alt="AI Vision Solver" />
  <img src="https://img.shields.io/badge/Dashboard-Dark%20Glass%20Real--Time%20(Port%208280)-00C7B7?style=for-the-badge" alt="Web Dashboard" />
</p>

---

## 🌟 Executive Overview

**LuckyWatch Fleet Engine** adalah platform otomasi terdistribusi berkinerja tinggi yang dirancang untuk mengeksekusi streaming task, menyelesaikan tantangan keamanan visual, mengklaim reward harian, dan mengelola penarikan saldo secara otonom di platform LuckyWatch.

Dibangun dengan arsitektur **100% Pure Python HTTP REST Client (Zero Browser)**, sistem ini beroperasi tanpa dependensi berat seperti Chromium, Selenium, atau Playwright, menghasilkan konsumsi memori sangat rendah (< 25MB per worker) dan latensi eksekusi sub-detik.

---

## 🏛️ System Architecture

```text
                                 ┌─────────────────────────────────────────┐
                                 │       MultiBotManager / CLI Core        │
                                 └────────────────────┬────────────────────┘
                                                      │
                       ┌──────────────────────────────┴──────────────────────────────┐
                       │                                                             │
        ┌──────────────▼──────────────┐                               ┌──────────────▼──────────────┐
        │  AccountWorker (Thread #1)  │                               │  AccountWorker (Thread #N)  │
        │   - Isolated Proxy (Node 01)│                               │   - Isolated Proxy (Node N) │
        │   - State & Session Cache   │                               │   - State & Session Cache   │
        └──────────────┬──────────────┘                               └──────────────┬──────────────┘
                       │                                                             │
        ┌──────────────┴──────────────────────────────┬──────────────────────────────┴──────────────┐
        │                                             │                                             │
 ┌──────▼──────────────┐                       ┌──────▼──────────────┐                       ┌──────▼──────────────┐
 │ Turnstile Solver API│                       │ AI Vision Solver    │                       │ LuckyWatch Upstream │
 │ (Port 5072 / Node)  │                       │ (Port 5073 / Vision)│                       │ REST & Task Stream  │
 └─────────────────────┘                       └─────────────────────┘                       └─────────────────────┘
                                                      ▲
                                                      │
                                       ┌──────────────┴──────────────────────────────┐
                                       │   Live Web Dashboard & Telemetry (Port 8280)│
                                       │   - Dark Glass UI / Cloudflare Tunnel       │
                                       │   - Multithreaded Upstream Fetcher (<0.3s)  │
                                       │   - Wallet Manager & OTP Checkpoint Unlock  │
                                       └─────────────────────────────────────────────┘
```

---

## 🚀 Fitur Mutakhir & Spesifikasi Arsitektur

### 1. 100% Pure Python HTTP REST Engine (Zero Browser) & Multi-Account Threading
* **Zero Browser Footprint:** Menghilangkan overhead rendering engine web browser. Eksekusi request murni menggunakan `urllib.request` teroptimasi dengan connection reuse.
* **Concurrent Multi-Threading:** Menggunakan model 1 dedicated worker thread (`AccountWorker`) per akun aktif dengan event loop non-blocking dan thread isolation.
* **Mobile Telemetry Emulation:** Mengirimkan payload signature perangkat mobile (Mali GPU hardware profile, viewport metrics, DPR 2.625, Linux armv81) untuk memastikan konsistensi verifikasi backend upstream.

### 2. Progressive Proxy Node Rotation & 5-Minute Cooldown Retry Loop
* **15-Node Egress Pool:** Otomatis mendistribusikan percobaan autentikasi ke node proxy lokal (Node 01 s/d 15 pada port 31001 hingga 31015).
* **Batch Failover Formula:** Menggunakan kalkulasi offset `(attempt_offset + i) % pool_size` untuk merotasi IP egress pada setiap percobaan login Cloudflare Turnstile.
* **Anti-Lockout Cooldown:** Jika 1 batch (10 percobaan) belum berhasil, worker secara otomatis masuk ke mode cooldown adaptif 300 detik (5 menit) sebelum mencoba batch berikutnya dengan rotasi node baru.

### 3. Multi-Tier Session Persistence (Anti-Redundant Login)
Menghindari login ulang yang tidak perlu dengan sistem verifikasi sesi 3 lapis sebelum memicu solver Turnstile:
1. **Tier 1 (User Profile REST):** Validasi token cookie aktif via endpoint `user/` (`getCurrentUser`).
2. **Tier 2 (Task Limits Stream):** Validasi autorisasi via `user/tasks/` (`getLimits`) yang tetap aktif meski profile endpoint mengalami security checkpoint.
3. **Tier 3 (Active Task Probe):** Validasi stream status via `user/tasks/` (`get`). Sesi dinyatakan valid jika server mengembalikan task aktif atau status kuota limit.

### 4. AI Vision Captcha Solver Integration (Port 5073) & In-Place Retry
* **Dedicated Vision Microservice:** Integrasi langsung ke microservice AI Vision pada `http://127.0.0.1:5073/solve` menggunakan model visual LLM untuk mendeteksi antrean ikon (queue) dan gambar utama (grid canvas).
* **Sub-Pixel Coordinate Mapping:** Solver mengembalikan koordinat presisi 3 klik `coor[0..2][x, y]` yang langsung disubmit ke `/api/user/captcha/check/`.
* **In-Place Refresh Handler:** Jika koordinat meleset dan server menyajikan challenge baru (`status: data`), bot langsung mengeksekusi re-solve instan tanpa mengulang durasi pemutaran video.
* **Closed-Loop Feedback:** Mengirimkan konfirmasi verifikasi (`verified: true`) ke `/feedback` solver untuk memperkuat akurasi dataset model.

### 5. Autonomous Background Auto-Withdrawal
* **Direct FaucetPay Payout:** Otomatis mentransfer reward ke wallet USDT TRC20 FaucetPay via `/api/user/payout/send/` segera setelah saldo memenuhi syarat minimum.
* **Configurable Threshold:** Nilai threshold dapat disesuaikan melalui `config.json` atau Web Dashboard (minimum `$0.10` USD).
* **1-Hour Anti-Spam Guard:** Mekanisme pengaman interval minimal 3600 detik antar transaksi penarikan per akun untuk mencegah red-flagging dari payment gateway.

### 6. Smart Daily Activity Bonus Claimer
* **Strict Tier Gating:** Memantau akumulasi view harian (`viewCurDay`) via `/api/user/tasks/dailyBonus/`.
* **Max Tier Prioritization:**
  * 100 views: 100 Clovers
  * 200 views: 500 Clovers
  * 300 views: 1000 Clovers
  * 400 views: $0.005 USD
  * **500 views: $0.010 USD (Tier Tertinggi)**
* **Policy Lock:** Sistem menahan klaim dan secara ketat menunggu hingga mencapai `500/500 views` untuk memastikan perolehan bonus maksimal $0.01 USD per hari UTC.

### 7. Real-Time Telemetry Dashboard (Port 8280)
* **Dark Glass UI:** Antarmuka modern berbasis Glassmorphism dengan Tailwind CSS, status badge real-time, visual live stream progress bar, dan per-account telemetry log.
* **High-Concurrency Upstream Fetcher:** Menggunakan `ThreadPoolExecutor` internal untuk mengambil live balance, daily bonus progress, dan wallet settings secara paralel dari upstream (< 0.3 detik).
* **Modal Wallet Manager:** Manajemen alamat wallet FaucetPay langsung dari browser dengan sinkronisasi otomatis ke `config.json` dan server upstream.
* **Email OTP Checkpoint Unlocker:** Tombol pemicu pengiriman ulang email konfirmasi (`reqConfirm`) untuk menyelesaikan verifikasi akun baru.
* **Integrated Service Control:** Aksi cepat restart background worker daemon via API endpoint `/api/actions/retry`.

### 8. Dynamic Sub-Second Scheduling & Precision Sleep
* **Hourly Rollover Sync (`:00:15`):** Saat batas `limitInHour` tercapai, worker menghitung selisih detik eksak hingga detik ke-15 pada jam berikutnya untuk langsung mengonsumsi kuota baru.
* **UTC Day Reset Sync (`00:00:30`):** Saat batas `limitInDay` tercapai, worker tidur hingga pergantian hari UTC (pukul 00:00:30 UTC).
* **Interruptible Adaptive Sleep:** Menggunakan pengecekan internal per 1 detik (`adaptive_sleep`) sehingga daemon dapat dimatikan secara instan tanpa blocking signal.

---

## 📁 Struktur Direktori

```text
luckywatch-bot/
├── bot.py                     # Core Multi-Account Threading & Streaming Engine
├── server.py                  # Telemetry Dashboard Server & Action REST API (Port 8280)
├── config.json                # Production Configuration (Accounts, Proxies, Thresholds)
├── config.example.json        # Template Konfigurasi
├── bot.log                    # Live Structured Telemetry Logs
├── state/
│   └── sessions.json          # Multi-Tier Cached Session Cookies & User States
└── README.md                  # Dokumentasi Arsitektur & Operasional
```

---

## ⚙️ Panduan Konfigurasi (`config.json`)

```json
{
  "accounts": [
    {
      "email": "user1@example.com",
      "password": "your_password",
      "proxy": "http://127.0.0.1:31001",
      "active": true,
      "faucetpay_usdt_trc20": "TYD6xK...your_wallet_address"
    },
    {
      "email": "user2@example.com",
      "password": "your_password",
      "proxy": "http://127.0.0.1:31002",
      "active": true,
      "faucetpay_usdt_trc20": ""
    }
  ],
  "proxy": {
    "enabled": true,
    "url": "http://127.0.0.1:31001",
    "health_check_url": "https://api.ipify.org?format=json"
  },
  "turnstile": {
    "solver_url": "http://127.0.0.1:5072",
    "sitekey": "0x4AAAAAABqiRMe3mbyG5xKO",
    "timeout_seconds": 35,
    "max_login_retries_per_batch": 10,
    "cooldown_on_failure_seconds": 300
  },
  "icon_solver": {
    "enabled": true,
    "solver_url": "http://127.0.0.1:5073/solve",
    "timeout_seconds": 60
  },
  "runner": {
    "delay_between_videos_seconds": 2,
    "auto_daily_bonus": true
  },
  "auto_withdraw": {
    "enabled": true,
    "threshold_usd": 0.10,
    "service": "faucetpayusdt"
  },
  "app": {
    "base_url": "https://luckywatch.pro",
    "signin_url": "https://luckywatch.pro/signin",
    "watch_url": "https://luckywatch.pro/watch"
  }
}
```

---

## 🛠️ Cara Menjalankan

### 1. Inisialisasi Environment
Pastikan Python 3.11+ telah terpasang. Seluruh engine dashboard dan bot menggunakan standard library tanpa modul eksternal wajib.

```bash
# Clone repositori
git clone https://github.com/IndraYuda13/luckywatch-bot.git
cd luckywatch-bot

# Siapkan file konfigurasi
cp config.example.json config.json
nano config.json
```

### 2. Menjalankan Engine Bot
```bash
# Menjalankan mode interaktif
python3 bot.py

# Menjalankan sebagai background daemon
python3 bot.py --daemon
```

### 3. Menjalankan Dashboard Telemetry
```bash
# Menjalankan dashboard pada port 8280
python3 server.py
```
Akses dashboard secara lokal pada `http://localhost:8280` atau melalui endpoint Cloudflare Tunnel yang telah dikonfigurasi.

### 4. Menjalankan via Systemd Daemon (Rekomendasi Produksi)
```bash
# Buat service unit /etc/systemd/system/luckywatch-bot.service
sudo systemctl daemon-reload
sudo systemctl enable --now luckywatch-bot.service
sudo systemctl status luckywatch-bot.service
```

---

## 📊 API & Telemetry Endpoints

Dashboard server menyediakan interface HTTP REST untuk integrasi pihak ketiga dan monitoring:

| Method | Endpoint | Deskripsi |
| :--- | :--- | :--- |
| `GET` | `/` | Antarmuka visual Dashboard Real-Time (Dark Glass UI) |
| `GET` | `/api/stats` | Telemetry agregat fleet, status worker, log per akun, & live balances |
| `POST` | `/api/actions/retry` | Me-restart background worker daemon secara aman |
| `POST` | `/api/actions/save_wallet` | Memperbarui wallet FaucetPay dan sinkronisasi ke upstream |
| `POST` | `/api/actions/send_verify_email` | Memicu pengiriman ulang OTP/link verifikasi email |
| `POST` | `/api/actions/withdraw` | Memicu penarikan saldo instan ke alamat wallet tersimpan |
| `POST` | `/api/actions/config_auto_withdraw` | Mengaktifkan/menonaktifkan auto-withdraw dan mengatur threshold |

---

## 🔒 Security & Safe Operations
* **Credential Isolation:** File `config.json` dan `state/sessions.json` diabaikan oleh `.gitignore` untuk mencegah kebocoran kredensial atau cookie sesi.
* **Masked Display:** Dashboard secara otomatis menyamarkan alamat email (`lv***e@gmail.com`) pada tampilan log publik dan antarmuka visual.
* **Non-Destructive Error Handling:** Kesalahan koneksi jaringan, limit kuota, atau tantangan captcha ditangani secara non-fatal dengan backoff eksponensial tanpa menghentikan thread akun lainnya.

---

## 📄 License
Private Repository. Proprietary automation software. All rights reserved.
