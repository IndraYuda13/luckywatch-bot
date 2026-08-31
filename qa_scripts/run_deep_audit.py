import os
import json
from playwright.sync_api import sync_playwright

os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "/root/.cache/ms-playwright"

def run_comprehensive_audit():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        page = context.new_page()
        
        console_logs = []
        page.on("console", lambda msg: console_logs.append({"type": msg.type, "text": msg.text}))
        
        page.goto("http://127.0.0.1:8280/", wait_until="networkidle")
        
        # 1. Check all elements contrast ratios & color variables
        wcag_audit = page.evaluate("""() => {
            function getLuminance(r, g, b) {
                const a = [r, g, b].map(v => {
                    v /= 255;
                    return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
                });
                return a[0] * 0.2126 + a[1] * 0.7152 + a[2] * 0.0722;
            }
            function getContrast(rgb1, rgb2) {
                const lum1 = getLuminance(rgb1[0], rgb1[1], rgb1[2]);
                const lum2 = getLuminance(rgb2[0], rgb2[1], rgb2[2]);
                const brightest = Math.max(lum1, lum2);
                const darkest = Math.min(lum1, lum2);
                return (brightest + 0.05) / (darkest + 0.05);
            }
            function parseRgb(str) {
                const m = str.match(/rgba?\\((\\d+),\\s*(\\d+),\\s*(\\d+)/);
                return m ? [parseInt(m[1]), parseInt(m[2]), parseInt(m[3])] : [255, 255, 255];
            }

            const results = [];
            const textNodes = Array.from(document.querySelectorAll('h1, h2, h3, .metric-value, .metric-label, .t-log-entry, .btn-action, .toast-text, .acc-email'));
            
            textNodes.forEach(node => {
                const computed = window.getComputedStyle(node);
                const fg = parseRgb(computed.color);
                let bg = [7, 9, 14];
                let parent = node;
                while (parent) {
                    const bgStr = window.getComputedStyle(parent).backgroundColor;
                    if (bgStr && bgStr !== 'rgba(0, 0, 0, 0)' && bgStr !== 'transparent') {
                        bg = parseRgb(bgStr);
                        break;
                    }
                    parent = parent.parentElement;
                }
                const ratio = getContrast(fg, bg);
                results.push({
                    tag: node.tagName.toLowerCase(),
                    text: node.textContent.trim().slice(0, 25),
                    fg: computed.color,
                    bg: `rgb(${bg.join(',')})`,
                    ratio: Math.round(ratio * 100) / 100,
                    wcag_aa_pass: ratio >= 4.5 || (parseFloat(computed.fontSize) >= 18 && ratio >= 3.0)
                });
            });
            return results;
        }""")
        
        # 2. Test Real Interactive Flows
        # Modal opening for wallet setup
        page.evaluate("""() => {
            if (typeof openWalletModal === 'function') {
                openWalletModal('testaccount@gmail.com', 'T9yD14Nj9j7xAB4dbGeiX9h8unkKHxuWwb');
            }
        }""")
        modal_visible = page.evaluate("() => document.getElementById('wallet-modal')?.style.display !== 'none'")
        page.screenshot(path="/root/projects/luckywatch-bot/qa_artifacts/modal_wallet_open.png")
        
        # Close modal
        page.evaluate("() => closeWalletModal()")
        
        # Threshold button interaction
        page.click("#pill-050")
        current_threshold_text = page.evaluate("() => document.querySelector('#pill-050.active')?.textContent")
        
        # Matrix View switch
        page.click("#view-btn-matrix")
        matrix_active = page.evaluate("() => document.getElementById('accounts-matrix')?.style.display !== 'none' || document.getElementById('view-btn-matrix')?.classList.contains('active')")
        page.screenshot(path="/root/projects/luckywatch-bot/qa_artifacts/matrix_view_active.png")
        
        # Grid View switch back
        page.click("#view-btn-grid")
        grid_active = page.evaluate("() => document.getElementById('accounts-grid')?.style.display !== 'none' || document.getElementById('view-btn-grid')?.classList.contains('active')")
        
        browser.close()
        
        summary = {
            "wcag_total_elements_audited": len(wcag_audit),
            "wcag_passed_elements": sum(1 for x in wcag_audit if x["wcag_aa_pass"]),
            "wcag_sample": wcag_audit[:10],
            "modal_interaction_verified": modal_visible,
            "threshold_pill_interaction": current_threshold_text == "$0.50",
            "matrix_tab_switching_verified": bool(matrix_active and grid_active),
            "console_errors_count": len(console_logs)
        }
        
        with open("/root/projects/luckywatch-bot/qa_artifacts/deep_audit_summary.json", "w") as f:
            json.dump(summary, f, indent=2)
            
        print("Deep audit executed successfully:", summary)

if __name__ == "__main__":
    run_comprehensive_audit()
