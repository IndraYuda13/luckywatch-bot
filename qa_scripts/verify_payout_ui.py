import json
import os
import sys
from playwright.sync_api import sync_playwright

os.makedirs("/root/projects/luckywatch-bot/qa_artifacts", exist_ok=True)

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    context = browser.new_context(viewport={"width": 1440, "height": 900})
    page = context.new_page()
    
    print("Navigating to http://127.0.0.1:8280 ...")
    page.goto("http://127.0.0.1:8280", wait_until="networkidle")
    page.wait_for_timeout(2500)
    
    # 1. Capture Grid Overview
    page.screenshot(path="/root/projects/luckywatch-bot/qa_artifacts/dashboard_payout_under_review_grid.png", full_page=True)
    print("Captured Grid Overview -> /root/projects/luckywatch-bot/qa_artifacts/dashboard_payout_under_review_grid.png")
    
    # 2. Check Under Review Badge in Grid
    review_badges = page.locator(".chip-review").all()
    print(f"Found {len(review_badges)} .chip-review badge(s) rendered in Grid view.")
    
    # 3. Check Under Review metric in header
    review_header = page.locator("#accounts-review-count").inner_text()
    print(f"Header Under Review count: {review_header}")
    
    # 4. Check Disabled Payout button state
    disabled_btns = page.locator("button:disabled").all()
    print(f"Found {len(disabled_btns)} disabled button(s).")
    
    # 5. Switch to Matrix View and capture
    page.click("#view-btn-matrix")
    page.wait_for_timeout(1000)
    page.screenshot(path="/root/projects/luckywatch-bot/qa_artifacts/dashboard_payout_under_review_matrix.png", full_page=True)
    print("Captured Matrix View -> /root/projects/luckywatch-bot/qa_artifacts/dashboard_payout_under_review_matrix.png")
    
    # 6. Mobile Viewport Check (390x844)
    page.set_viewport_size({"width": 390, "height": 844})
    page.wait_for_timeout(1000)
    page.screenshot(path="/root/projects/luckywatch-bot/qa_artifacts/dashboard_payout_under_review_mobile.png", full_page=True)
    print("Captured Mobile View -> /root/projects/luckywatch-bot/qa_artifacts/dashboard_payout_under_review_mobile.png")
    
    browser.close()
    print("Visual verification script completed successfully.")
