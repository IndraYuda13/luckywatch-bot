import os
import json
import time
from playwright.sync_api import sync_playwright

os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "/root/.cache/ms-playwright"

def run_stress_and_interaction_verification():
    artifacts_dir = "/root/projects/luckywatch-bot/qa_artifacts"
    os.makedirs(artifacts_dir, exist_ok=True)
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        
        console_logs = []
        page.on("console", lambda msg: console_logs.append({"type": msg.type, "text": msg.text}))
        
        page.goto("http://127.0.0.1:8280/", wait_until="networkidle")
        
        # 1. State-Aware Toasts Sequential Trigger & Visual Confirmation
        toast_types = ["success", "error", "warning", "info"]
        toast_captures = []
        for t in toast_types:
            page.evaluate(f"showToast('Synthetic {t.upper()} verification message', '{t}')")
            time.sleep(0.2)
            page.screenshot(path=f"{artifacts_dir}/toast_live_{t}.png")
            toast_data = page.evaluate(f"""() => {{
                const el = document.querySelector('.toast-{t}');
                return {{
                    visible: !!el,
                    color: window.getComputedStyle(el).color,
                    borderColor: window.getComputedStyle(el).borderColor
                }};
            }}""")
            toast_captures.append({t: toast_data})
            
        # 2. FIFO 200 items Stress Test
        page.evaluate("""() => {
            const lines = [];
            for (let i = 1; i <= 500; i++) {
                lines.push(`[2026-08-31 12:15:${i}] Stress item #${i} - Live throughput test`);
            }
            globalState = globalState || {};
            globalState.logs = lines;
            renderLogs();
        }""")
        
        fifo_count = page.evaluate("() => document.querySelectorAll('#terminal-stream-window .t-log-entry').length")
        first_node = page.evaluate("() => document.querySelectorAll('#terminal-stream-window .t-log-entry')[0]?.textContent")
        last_node = page.evaluate("() => document.querySelectorAll('#terminal-stream-window .t-log-entry')[199]?.textContent")
        
        page.screenshot(path=f"{artifacts_dir}/log_fifo_stress_500_to_200.png")
        
        # 3. Log level filtering verification
        page.evaluate("""() => {
            const lines = [
                "[2026-08-31 12:00:01] [INFO] Connection established",
                "[2026-08-31 12:00:02] [SUCCESS] REWARD claimed: +10.00 views",
                "[2026-08-31 12:00:03] [WARNING] Sleeping for 45s",
                "[2026-08-31 12:00:04] [ERROR] Upstream gateway failed",
            ];
            globalState.logs = lines;
            handleLogLevelChange('SUCCESS');
        }""")
        success_filter_count = page.evaluate("() => document.querySelectorAll('#terminal-stream-window .t-log-entry').length")
        
        page.evaluate("handleLogLevelChange('ERROR')")
        error_filter_count = page.evaluate("() => document.querySelectorAll('#terminal-stream-window .t-log-entry').length")
        
        page.evaluate("handleLogLevelChange('ALL')")
        all_filter_count = page.evaluate("() => document.querySelectorAll('#terminal-stream-window .t-log-entry').length")
        
        # 4. Viewport Breakpoints & Snapshots
        viewports = [
            ("mobile_360x740", 360, 740),
            ("mobile_390x844", 390, 844),
            ("tablet_768x1024", 768, 1024),
            ("laptop_1280x800", 1280, 800),
            ("desktop_1440x900", 1440, 900),
            ("ultrawide_1920x1080", 1920, 1080)
        ]
        
        viewport_results = []
        for name, w, h in viewports:
            page.set_viewport_size({"width": w, "height": h})
            time.sleep(0.2)
            page.screenshot(path=f"{artifacts_dir}/viewport_{name}.png", full_page=True)
            has_h_scroll = page.evaluate("() => document.documentElement.scrollWidth > window.innerWidth")
            viewport_results.append({
                "viewport": name,
                "width": w,
                "height": h,
                "horizontal_overflow": has_h_scroll
            })
            
        summary = {
            "toasts": toast_captures,
            "fifo_dom_nodes_after_500_inputs": fifo_count,
            "fifo_first_node": first_node,
            "fifo_last_node": last_node,
            "filters": {
                "success_only": success_filter_count,
                "error_only": error_filter_count,
                "all": all_filter_count
            },
            "viewports": viewport_results,
            "console_errors_count": len(console_logs)
        }
        
        with open(f"{artifacts_dir}/stress_and_interaction_summary.json", "w") as f:
            json.dump(summary, f, indent=2)
            
        print("Stress and interaction verification finished successfully!")
        browser.close()

if __name__ == "__main__":
    run_stress_and_interaction_verification()
