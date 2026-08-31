import re
from pathlib import Path

def test_payout_history_dual_view_contract():
    template_path = Path("/root/projects/luckywatch-bot/dashboard_template.html")
    server_path = Path("/root/projects/luckywatch-bot/server.py")
    
    assert template_path.exists()
    assert server_path.exists()
    
    html_t = template_path.read_text(encoding="utf-8")
    py_s = server_path.read_text(encoding="utf-8")
    
    # 1. Check semantic color tokens and icons in getPayoutBadge
    for code_str in [html_t, py_s]:
        # Status 1: PAID (#34D399, ✓, rgba(16, 185, 129, 0.12), rgba(16, 185, 129, 0.35))
        assert "#34D399" in code_str
        assert "rgba(16, 185, 129, 0.12)" in code_str
        assert "rgba(16, 185, 129, 0.35)" in code_str
        
        # Status 2: IN PROGRESS (#38BDF8, ⚡, rgba(6, 182, 212, 0.12), rgba(6, 182, 212, 0.35))
        assert "#38BDF8" in code_str
        assert "rgba(6, 182, 212, 0.12)" in code_str
        assert "rgba(6, 182, 212, 0.35)" in code_str
        
        # Status 3: UNDER REVIEW (#FDE047, ⏳, rgba(245, 158, 11, 0.15), rgba(245, 158, 11, 0.50))
        assert "#FDE047" in code_str
        assert "rgba(245, 158, 11, 0.15)" in code_str
        assert "rgba(245, 158, 11, 0.50)" in code_str
        
        # Status 0: PAYMENT ERROR (#FB7185, ⚠️, rgba(244, 63, 94, 0.12), rgba(244, 63, 94, 0.35))
        assert "#FB7185" in code_str
        assert "rgba(244, 63, 94, 0.12)" in code_str
        assert "rgba(244, 63, 94, 0.35)" in code_str
        
        # Null / Empty: No payout history yet
        assert "No payout history yet" in code_str
        
        # Sanitization: escapeHtml usage on payout fields
        assert "escapeHtml(String(acc.last_payout.id))" in code_str
        assert "escapeHtml(String(acc.last_payout.amount" in code_str
        assert "escapeHtml(String(acc.last_payout.timestamp" in code_str

    # 2. Check Matrix Table header contains "Last Payout"
    assert "<th>Last Payout</th>" in html_t
    assert "<th>Last Payout</th>" in py_s
