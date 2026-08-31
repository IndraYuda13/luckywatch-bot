import os
from playwright.sync_api import sync_playwright

os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "/root/.cache/ms-playwright"

def detailed_wcag_audit():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.goto("http://127.0.0.1:8280/", wait_until="networkidle")
        
        report = page.evaluate("""() => {
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
            function parseRgba(str) {
                const m = str.match(/rgba?\\((\\d+),\\s*(\\d+),\\s*(\\d+)(?:,\\s*([\\d.]+))?\\)/);
                return m ? [parseInt(m[1]), parseInt(m[2]), parseInt(m[3]), m[4] !== undefined ? parseFloat(m[4]) : 1.0] : [255, 255, 255, 1.0];
            }
            function blend(fgRgba, bgRgb) {
                const alpha = fgRgba[3];
                return [
                    Math.round((1 - alpha) * bgRgb[0] + alpha * fgRgba[0]),
                    Math.round((1 - alpha) * bgRgb[1] + alpha * fgRgba[1]),
                    Math.round((1 - alpha) * bgRgb[2] + alpha * fgRgba[2])
                ];
            }
            function getEffectiveBg(el) {
                let current = el;
                let bgStack = [];
                while (current) {
                    const comp = window.getComputedStyle(current);
                    const bg = parseRgba(comp.backgroundColor);
                    if (bg[3] > 0) {
                        bgStack.unshift(bg);
                    }
                    current = current.parentElement;
                }
                // Base background is #07090E
                let effective = [7, 9, 14];
                for (const layer of bgStack) {
                    effective = blend(layer, effective);
                }
                return effective;
            }

            const elements = Array.from(document.querySelectorAll('h1, h2, h3, .metric-value, .metric-label, .t-log-entry, button, .preset-pill, .toast-text, .acc-email, .badge, .status-pill'));
            const results = [];
            
            elements.forEach((el, i) => {
                const text = el.innerText?.trim();
                if (!text) return;
                const comp = window.getComputedStyle(el);
                if (comp.display === 'none' || comp.visibility === 'hidden') return;
                
                const fgRgba = parseRgba(comp.color);
                const effectiveBg = getEffectiveBg(el);
                const effectiveFg = blend(fgRgba, effectiveBg);
                const contrast = getContrast(effectiveFg, effectiveBg);
                const fontSize = parseFloat(comp.fontSize);
                const isBold = parseInt(comp.fontWeight) >= 700 || comp.fontWeight === 'bold';
                const isLargeText = fontSize >= 24 || (fontSize >= 18.66 && isBold);
                const minReq = isLargeText ? 3.0 : 4.5;
                
                results.push({
                    text: text.slice(0, 30),
                    tag: el.tagName.toLowerCase(),
                    fontSize,
                    isLargeText,
                    contrast: Math.round(contrast * 100) / 100,
                    minRequired: minReq,
                    pass: contrast >= minReq
                });
            });
            return results;
        }""")
        
        total = len(report)
        passed = sum(1 for r in report if r["pass"])
        failed = [r for r in report if not r["pass"]]
        print(f"Total checked: {total}, Passed: {passed}, Failed: {len(failed)}")
        if failed:
            print("Failed sample:", failed[:5])
        browser.close()

if __name__ == "__main__":
    detailed_wcag_audit()
