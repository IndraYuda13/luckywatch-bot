import pytest
from pathlib import Path
import json

def test_dashboard_template_contains_under_review_badging_and_safety_lock():
    template_path = Path("/root/projects/luckywatch-bot/dashboard_template.html")
    assert template_path.exists(), "dashboard_template.html must exist"
    content = template_path.read_text(encoding="utf-8")
    
    # 1. Chip review styling
    assert ".chip-review" in content, "CSS must define .chip-review for Under Review badging"
    assert "⏳ UNDER REVIEW" in content, "Grid and Matrix must render ⏳ UNDER REVIEW status text"
    
    # 2. Tooltip explaining under review pause
    tooltip_text = "Payout is currently under review by LuckyWatch. Auto-withdraw paused."
    assert tooltip_text in content, "Template must include exact Under Review tooltip string"
    
    # 3. Action button safety lock (disable manual payout button when under review)
    assert "isUnderReview" in content, "Template JavaScript must compute isUnderReview state"
    assert "⏳ In Review" in content or "⏳" in content, "Button must show review indicator when in review"
    
    # 4. Header summary counter for under review accounts
    assert "accounts-review-count" in content, "Template must include accounts-review-count DOM element in Withdrawal Hub"
    assert "Under Review" in content, "Withdrawal Hub header must have Under Review metric label"


def test_server_summary_under_review_count():
    from server import get_latest_stats
    stats = get_latest_stats()
    assert "summary" in stats, "Stats must include summary dict"
    assert "under_review_count" in stats["summary"], "summary must include under_review_count metric"
    assert isinstance(stats["summary"]["under_review_count"], int), "under_review_count must be integer"
    
    # Check that accounts array carries payout_status and payout_under_review
    assert "accounts" in stats, "Stats must include accounts list"
    for acc in stats["accounts"]:
        assert "payout_status" in acc, "Every account object must have payout_status"
        assert "payout_under_review" in acc, "Every account object must have payout_under_review flag"
        assert "payout_backoff_until" in acc, "Every account object must have payout_backoff_until"
