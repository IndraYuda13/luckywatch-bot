import os
from playwright.sync_api import sync_playwright

os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "/root/.cache/ms-playwright"

def check_gold_button_contrast():
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

            const btn = document.getElementById('btn-withdraw-all');
            const comp = window.getComputedStyle(btn);
            
            // Gold gradient: #FDE047 (253, 224, 71) to #CA8A04 (202, 138, 4)
            // Color: #000 (0, 0, 0)
            const lumText = getLuminance(0, 0, 0); // 0
            const lumGold1 = getLuminance(253, 224, 71); // high luminance
            const lumGold2 = getLuminance(202, 138, 4); // medium luminance
            
            const contrast1 = (lumGold1 + 0.05) / (lumText + 0.05);
            const contrast2 = (lumGold2 + 0.05) / (lumText + 0.05);
            
            return {
                text: btn.innerText,
                color: comp.color,
                backgroundImage: comp.backgroundImage,
                contrast_top_gold: contrast1,
                contrast_bottom_gold: contrast2
            };
        }""")
        print("Gold button contrast:", info)
        browser.close()

if __name__ == "__main__":
    check_gold_button_contrast()
