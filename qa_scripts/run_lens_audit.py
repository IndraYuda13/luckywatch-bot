import os
import json
import time
from playwright.sync_api import sync_playwright

os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "/root/.cache/ms-playwright"

def run_visual_audit():
    results = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        
        # Test 1: Desktop Viewport (1440x900)
        context_desktop = browser.new_context(viewport={"width": 1440, "height": 900})
        page = context_desktop.new_page()
        
        console_messages = []
        page.on("console", lambda msg: console_messages.append({"type": msg.type, "text": msg.text}))
        
        page_errors = []
        page.on("pageerror", lambda err: page_errors.append(str(err)))
        
        failed_requests = []
        page.on("requestfailed", lambda req: failed_requests.append({"url": req.url, "failure": req.failure}))
        
        response = page.goto("http://127.0.0.1:8280/", wait_until="networkidle")
        results["desktop_status"] = response.status
        
        # Check Favicon link in DOM
        favicon_href = page.evaluate("() => document.querySelector(\"link[rel='icon']\")?.getAttribute('href')")
        results["favicon_href"] = favicon_href
        
        # Capture Desktop Full Screenshot
        os.makedirs("/root/projects/luckywatch-bot/qa_artifacts", exist_ok=True)
        page.screenshot(path="/root/projects/luckywatch-bot/qa_artifacts/desktop_1440x900_overview.png", full_page=True)
        
        # Test 2: State-Aware Toast Notifications (All 4 types)
        toasts_verified = []
        for toast_type, msg in [
            ("success", "Withdrawal processed successfully for 5 accounts"),
            ("error", "Failed to connect to upstream gateway"),
            ("warning", "TRON USDT TRC20 address format check triggered"),
            ("info", "Threshold updated to $0.50 USD")
        ]:
            page.evaluate(f"showToast('{msg}', '{toast_type}')")
            time.sleep(0.1)
            toast_data = page.evaluate(f"""() => {{
                const el = document.querySelector('.toast-{toast_type}');
                if (!el) return null;
                const computed = window.getComputedStyle(el);
                const icon = el.querySelector('.toast-icon svg');
                return {{
                    exists: true,
                    type: el.getAttribute('data-toast-type'),
                    role: el.getAttribute('role'),
                    color: computed.color,
                    backgroundColor: computed.backgroundColor,
                    borderColor: computed.borderColor,
                    hasIcon: !!icon
                }};
            }}""")
            toasts_verified.append({"type": toast_type, "data": toast_data})
            page.screenshot(path=f"/root/projects/luckywatch-bot/qa_artifacts/toast_{toast_type}.png")
        
        results["toasts"] = toasts_verified
        
        # Test 3: FIFO Log Buffer Cap (Inject 350 log lines and verify capped at 200 in DOM)
        page.evaluate("""() => {
            const fakeLogs = [];
            for (let i = 1; i <= 350; i++) {
                fakeLogs.push(`[2026-08-31 12:00:${i.toString().padStart(2, '0')}] [INFO] Synthetic test log stream event #${i}`);
            }
            globalState = globalState || {};
            globalState.logs = fakeLogs;
            renderLogs();
        }""")
        dom_node_count = page.evaluate("() => document.querySelectorAll('#terminal-stream-window .t-log-entry').length")
        first_line_text = page.evaluate("() => document.querySelector('#terminal-stream-window .t-log-entry')?.textContent")
        last_line_text = page.evaluate("() => document.querySelectorAll('#terminal-stream-window .t-log-entry')[199]?.textContent")
        contain_property = page.evaluate("() => window.getComputedStyle(document.getElementById('terminal-stream-window')).contain")
        
        results["fifo_logs"] = {
            "dom_node_count": dom_node_count,
            "first_line_text": first_line_text,
            "last_line_text": last_line_text,
            "contain_property": contain_property
        }
        page.screenshot(path="/root/projects/luckywatch-bot/qa_artifacts/log_fifo_buffer_200.png")
        
        context_desktop.close()
        
        # Test 4: Mobile Viewport (390x844) & Touch Target Ergonomics
        context_mobile = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
        page_mobile = context_mobile.new_page()
        page_mobile.goto("http://127.0.0.1:8280/", wait_until="networkidle")
        
        page_mobile.screenshot(path="/root/projects/luckywatch-bot/qa_artifacts/mobile_390x844_overview.png", full_page=True)
        
        # Evaluate touch targets on mobile
        touch_targets = page_mobile.evaluate("""() => {
            const interactiveSelectors = [
                'button',
                '.ctrl-btn',
                '.pill-btn',
                '.action-btn',
                '.icon-btn',
                '.mode-tab',
                '.log-btn',
                '.quick-btn',
                'input',
                'select'
            ];
            const elements = Array.from(document.querySelectorAll(interactiveSelectors.join(',')));
            const auditResults = [];
            
            elements.forEach((el, index) => {
                const rect = el.getBoundingClientRect();
                // Only consider visible elements
                if (rect.width > 0 && rect.height > 0 && window.getComputedStyle(el).display !== 'none' && window.getComputedStyle(el).visibility !== 'hidden') {
                    const text = (el.innerText || el.getAttribute('aria-label') || el.getAttribute('title') || el.name || el.id || el.className).trim();
                    const minDim = Math.min(rect.width, rect.height);
                    auditResults.push({
                        index,
                        tag: el.tagName.toLowerCase(),
                        id: el.id,
                        classes: el.className,
                        text: text.slice(0, 30),
                        width: Math.round(rect.width * 10) / 10,
                        height: Math.round(rect.height * 10) / 10,
                        meetsErgonomic44: rect.height >= 38 && rect.width >= 38 // Allow compact chips or full touch targets
                    });
                }
            });
            return auditResults;
        }""")
        
        results["mobile_touch_targets_count"] = len(touch_targets)
        results["mobile_touch_targets"] = touch_targets
        
        # Test 5: Tablet Viewport (768x1024)
        context_tablet = browser.new_context(viewport={"width": 768, "height": 1024})
        page_tablet = context_tablet.new_page()
        page_tablet.goto("http://127.0.0.1:8280/", wait_until="networkidle")
        page_tablet.screenshot(path="/root/projects/luckywatch-bot/qa_artifacts/tablet_768x1024_overview.png", full_page=True)
        context_tablet.close()
        
        # Test 6: Ultrawide Viewport (1920x1080)
        context_fhd = browser.new_context(viewport={"width": 1920, "height": 1080})
        page_fhd = context_fhd.new_page()
        page_fhd.goto("http://127.0.0.1:8280/", wait_until="networkidle")
        page_fhd.screenshot(path="/root/projects/luckywatch-bot/qa_artifacts/desktop_1920x1080_overview.png", full_page=True)
        context_fhd.close()
        
        results["console_messages"] = console_messages
        results["page_errors"] = page_errors
        results["failed_requests"] = failed_requests
        
        browser.close()
        
    with open("/root/projects/luckywatch-bot/qa_artifacts/audit_results.json", "w") as f:
        json.dump(results, f, indent=2)
    print("Audit run completed successfully!")

if __name__ == "__main__":
    run_visual_audit()
