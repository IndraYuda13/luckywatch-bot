import os
from playwright.sync_api import sync_playwright

os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "/root/.cache/ms-playwright"

def check_button_contrast():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.goto("http://127.0.0.1:8280/", wait_until="networkidle")
        
        info = page.evaluate("""() => {
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
                const m = str.match(/rgba?\\((\\d+),\\s*(\\d+),\\s*(\\d+)(?:,\\s*([\\d.]+))?\\)/);
                return m ? [parseInt(m[1]), parseInt(m[2]), parseInt(m[3]), m[4] !== undefined ? parseFloat(m[4]) : 1.0] : [255, 255, 255, 1.0];
            }

            const btn = document.querySelector('.btn-action');
            const comp = window.getComputedStyle(btn);
            const fg = parseRgb(comp.color);
            const bg = parseRgb(comp.backgroundColor);
            
            return {
                text: btn.innerText,
                fg: comp.color,
                bg: comp.backgroundColor,
                parsed_fg: fg,
                parsed_bg: bg,
                contrast_vs_body: getContrast(fg, [7, 9, 14]),
                contrast_vs_own_bg: getContrast(fg, bg)
            };
        }""")
        print("Button contrast evaluation:", info)
        browser.close()

if __name__ == "__main__":
    check_button_contrast()
