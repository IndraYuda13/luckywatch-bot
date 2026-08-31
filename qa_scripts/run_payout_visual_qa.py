import json
import os
import sys
import time
from playwright.sync_api import sync_playwright

os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "/root/.cache/ms-playwright"
ARTIFACTS_DIR = "/root/projects/luckywatch-bot/qa_artifacts"
os.makedirs(ARTIFACTS_DIR, exist_ok=True)

VIEWPORTS = [
    {"name": "desktop_1440x900", "width": 1440, "height": 900, "label": "Desktop (1440px)"},
    {"name": "laptop_1280x800", "width": 1280, "height": 800, "label": "Laptop (1280px)"},
    {"name": "tablet_768x1024", "width": 768, "height": 1024, "label": "Tablet Portrait (768px)"},
    {"name": "mobile_390x844", "width": 390, "height": 844, "label": "Modern Mobile (390px)"},
    {"name": "mobile_360x740", "width": 360, "height": 740, "label": "Compact Mobile (360px)"},
]

STATUS_STATES = [
    {
        "status_code": "1",
        "status": "PAID",
        "expected_label": "PAID",
        "expected_color": "#34D399",
        "amount": "0.10000",
        "net_amount": "0.09800",
        "id": 9821,
        "timestamp": "2026-08-31 12:45:10",
        "wallet": "TQn9...4v8k",
        "desc": "Emerald / PAID status"
    },
    {
        "status_code": "2",
        "status": "IN PROGRESS",
        "expected_label": "IN PROGRESS",
        "expected_color": "#38BDF8",
        "amount": "0.50000",
        "net_amount": "0.49000",
        "id": 9822,
        "timestamp": "2026-08-31 13:02:15",
        "wallet": "TRb2...9m1x",
        "desc": "Cyan / IN PROGRESS status"
    },
    {
        "status_code": "3",
        "status": "UNDER REVIEW",
        "expected_label": "UNDER REVIEW",
        "expected_color": "#FDE047",
        "amount": "1.00000",
        "net_amount": "0.98000",
        "id": 9823,
        "timestamp": "2026-08-31 13:10:00",
        "wallet": "TLx4...7a2q",
        "desc": "Warm Gold / UNDER REVIEW status"
    },
    {
        "status_code": "0",
        "status": "PAYMENT ERROR",
        "expected_label": "PAYMENT ERROR",
        "expected_color": "#FB7185",
        "amount": "0.10000",
        "net_amount": "0.00000",
        "id": 9824,
        "timestamp": "2026-08-31 13:15:22",
        "wallet": "TQn9...4v8k",
        "desc": "Rose / PAYMENT ERROR status"
    },
]

results = {
    "viewports": {},
    "status_chips": {},
    "cache_headers": {},
    "console_messages": [],
    "layout_overflow": {},
    "touch_targets": {},
    "views": {}
}

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    
    # 1. First test live default state across viewports
    for vp in VIEWPORTS:
        context = browser.new_context(viewport={"width": vp["width"], "height": vp["height"]})
        page = context.new_page()
        
        console_logs = []
        page.on("console", lambda msg: console_logs.append({"type": msg.type, "text": msg.text}))
        page.on("pageerror", lambda err: console_logs.append({"type": "pageerror", "text": str(err)}))
        
        response = page.goto("http://127.0.0.1:8280/", wait_until="domcontentloaded")
        page.wait_for_timeout(2500)
        
        # Check cache headers
        if not results["cache_headers"] and response:
            results["cache_headers"] = {
                "status": response.status,
                "headers": dict(response.headers)
            }
            
        # Check overflow
        overflow = page.evaluate("() => ({ scrollWidth: document.documentElement.scrollWidth, clientWidth: document.documentElement.clientWidth, overflowX: document.documentElement.scrollWidth > document.documentElement.clientWidth })")
        results["layout_overflow"][vp["name"]] = overflow
        
        # Capture screenshot
        shot_path = f"{ARTIFACTS_DIR}/payout_vp_{vp['name']}_grid.png"
        page.screenshot(path=shot_path, full_page=True)
        results["viewports"][vp["name"]] = {
            "screenshot": shot_path,
            "overflow": overflow,
            "console_count": len(console_logs)
        }
        
        # Test Matrix View in Desktop/Tablet/Mobile
        if vp["width"] in (1440, 768, 390):
            page.click("#view-btn-matrix")
            page.wait_for_timeout(600)
            matrix_shot = f"{ARTIFACTS_DIR}/payout_vp_{vp['name']}_matrix.png"
            page.screenshot(path=matrix_shot, full_page=True)
            results["views"][f"{vp['name']}_matrix"] = matrix_shot
            
        context.close()

    # 2. Test Apple-Grade Dual-View Payout History with 4 Semantic Status Chips
    context = browser.new_context(viewport={"width": 1440, "height": 900})
    page = context.new_page()
    page.goto("http://127.0.0.1:8280/", wait_until="domcontentloaded")
    page.wait_for_timeout(2000)
    
    # Inject synthetic account states covering all 4 status chip colors simultaneously
    synthetic_accounts = []
    for idx, st in enumerate(STATUS_STATES):
        acc = {
            "email": f"worker{idx+1}@testmail.com",
            "email_redacted": f"w***{idx+1}@testmail.com",
            "country": "ID" if idx % 2 == 0 else "SG",
            "country_name": "Indonesia" if idx % 2 == 0 else "Singapore",
            "egress_ip": f"103.147.154.{10+idx}",
            "balance": f"{(idx+1)*0.35:.7f}",
            "clovers": 2500 * (idx + 1),
            "status": "ACTIVE",
            "payout_status": st["status"],
            "payout_under_review": (st["status"] in ("UNDER REVIEW", "IN PROGRESS")),
            "daily_done": 250,
            "daily_cap": 560,
            "hourly_done": 30,
            "hourly_cap": 65,
            "email_verified": True,
            "daily_bonus_claimed": True,
            "faucetpay_usdt_trc20": "TQn9xYg6U2bV6j...TRC20",
            "server_wallet_set": True,
            "last_activity_time": "13:20:10",
            "last_payout": {
                "id": st["id"],
                "status_code": st["status_code"],
                "status": st["status"],
                "amount": st["amount"],
                "net_amount": st["net_amount"],
                "timestamp": st["timestamp"],
                "wallet": st["wallet"]
            }
        }
        synthetic_accounts.append(acc)

    # Inject into globalState and re-render
    page.evaluate("""(accounts) => {
        if (!globalState) {
            globalState = {
                summary: { total_balance: '1.4000000', total_clovers: 25000, total_tasks_today: 120, active_workers: 4, sleeping_workers: 0, total_accounts: 4 },
                auto_withdraw: { enabled: false, threshold_usd: 0.10 },
                service_status: 'FLEET RUNNING 24/7',
                updated_at: '2026-08-31 13:20:00',
                accounts: accounts,
                logs: []
            };
        } else {
            globalState.accounts = accounts;
            globalState.summary.total_accounts = accounts.length;
        }
        renderHUD(globalState);
        renderWithdrawalHub(globalState);
        renderAccounts(accounts);
    }""", synthetic_accounts)
    page.wait_for_timeout(800)

    # Verify Card Grid View with 4 status chips
    card_chips_info = page.evaluate("""() => {
        const cards = document.querySelectorAll('.account-card');
        const data = [];
        cards.forEach((c, idx) => {
            const strip = c.querySelector('.payout-card-strip');
            const chip = strip ? strip.querySelector('.status-badge-chip') : null;
            const amountEl = strip ? strip.querySelector('.tabular') : null;
            data.push({
                index: idx,
                has_strip: !!strip,
                chip_text: chip ? chip.innerText.trim() : null,
                amount_text: amountEl ? amountEl.innerText.trim() : null,
                strip_bg: strip ? window.getComputedStyle(strip).backgroundColor : null,
                strip_border: strip ? window.getComputedStyle(strip).borderColor : null,
                chip_color: chip ? window.getComputedStyle(chip).color : null
            });
        });
        return data;
    }""")
    results["status_chips"]["card_grid"] = card_chips_info

    # Capture 4-chip Grid Screenshot
    grid_4chip_path = f"{ARTIFACTS_DIR}/payout_dual_view_4chips_grid.png"
    page.screenshot(path=grid_4chip_path, full_page=True)
    results["views"]["dual_view_4chips_grid"] = grid_4chip_path

    # Switch to Matrix View with 4 status chips
    # Re-apply synthetic accounts to ensure matrix view renders them before fetchStats overrides
    page.evaluate("""(accounts) => {
        globalState.accounts = accounts;
        globalState.summary.total_accounts = accounts.length;
        renderAccounts(accounts);
    }""", synthetic_accounts)
    page.click("#view-btn-matrix")
    page.wait_for_timeout(800)
    page.evaluate("""(accounts) => {
        globalState.accounts = accounts;
        renderAccounts(accounts);
    }""", synthetic_accounts)
    page.wait_for_timeout(400)
    
    matrix_chips_info = page.evaluate("""() => {
        const rows = document.querySelectorAll('#matrix-tbody tr');
        const data = [];
        rows.forEach((r, idx) => {
            const cells = r.querySelectorAll('td');
            const payoutCell = cells[6];
            const chip = payoutCell ? payoutCell.querySelector('.status-badge-chip') : null;
            const amountEl = payoutCell ? payoutCell.querySelector('.tabular') : null;
            data.push({
                index: idx,
                has_cell: !!payoutCell,
                chip_text: chip ? chip.innerText.trim() : null,
                amount_text: amountEl ? amountEl.innerText.trim() : null,
                chip_color: chip ? window.getComputedStyle(chip).color : null
            });
        });
        return data;
    }""")
    results["status_chips"]["matrix_table"] = matrix_chips_info

    # Capture 4-chip Matrix Screenshot
    matrix_4chip_path = f"{ARTIFACTS_DIR}/payout_dual_view_4chips_matrix.png"
    page.screenshot(path=matrix_4chip_path, full_page=True)
    results["views"]["dual_view_4chips_matrix"] = matrix_4chip_path

    # Check Mobile 390px view of the 4 chips
    page.set_viewport_size({"width": 390, "height": 844})
    page.click("#view-btn-grid")
    page.wait_for_timeout(800)
    mobile_4chip_path = f"{ARTIFACTS_DIR}/payout_dual_view_4chips_mobile.png"
    page.screenshot(path=mobile_4chip_path, full_page=True)
    results["views"]["dual_view_4chips_mobile"] = mobile_4chip_path

    # Check touch targets on Mobile
    touch_metrics = page.evaluate("""() => {
        const buttons = Array.from(document.querySelectorAll('button, input, select, .preset-pill, .status-badge-chip'));
        const smallTargets = [];
        buttons.forEach(btn => {
            const rect = btn.getBoundingClientRect();
            if (rect.width > 0 && rect.height > 0) {
                if (rect.width < 44 || rect.height < 44) {
                    // Check if it's an inline badge or a real button
                    smallTargets.push({
                        tag: btn.tagName,
                        cls: btn.className,
                        text: btn.innerText.slice(0, 20),
                        width: Math.round(rect.width),
                        height: Math.round(rect.height)
                    });
                }
            }
        });
        return {
            total_elements: buttons.length,
            small_targets: smallTargets
        };
    }""")
    results["touch_targets"] = touch_metrics

    browser.close()

# Save structured summary JSON
with open(f"{ARTIFACTS_DIR}/payout_visual_qa_summary.json", "w") as f:
    json.dump(results, f, indent=2)

print("Comprehensive Visual QA execution finished successfully!")
print(f"Summary artifact -> {ARTIFACTS_DIR}/payout_visual_qa_summary.json")
