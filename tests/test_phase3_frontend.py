import pytest
from pathlib import Path
import re

def test_dashboard_template_contains_state_aware_toasts():
    path = Path("/root/projects/luckywatch-bot/dashboard_template.html")
    assert path.exists()
    content = path.read_text(encoding="utf-8")
    
    # Check toast types
    assert "toast-success" in content
    assert "toast-error" in content
    assert "toast-warning" in content
    assert "toast-info" in content
    assert "TOAST_ICONS" in content

def test_dashboard_template_contains_fifo_log_buffer_and_containment():
    path = Path("/root/projects/luckywatch-bot/dashboard_template.html")
    content = path.read_text(encoding="utf-8")
    
    # Check bounded DOM nodes cap
    assert "MAX_DOM_LOG_NODES = 200" in content
    assert "slice(-MAX_DOM_LOG_NODES)" in content
    
    # Check DOM containment
    assert "contain: content;" in content

def test_dashboard_template_mobile_touch_targets():
    path = Path("/root/projects/luckywatch-bot/dashboard_template.html")
    content = path.read_text(encoding="utf-8")
    
    # Check 44x44 mobile touch targets in media query
    assert "@media (max-width: 640px)" in content
    assert "min-height: 44px;" in content
    assert "min-width: 44px;" in content

def test_dashboard_template_favicon_and_wcag_contrast():
    path = Path("/root/projects/luckywatch-bot/dashboard_template.html")
    content = path.read_text(encoding="utf-8")
    
    # Check SVG Favicon
    assert "data:image/svg+xml" in content
    assert "rel=\"icon\"" in content or "rel='icon'" in content
    
    # Check WCAG AA contrast for tertiary text
    assert "--text-tertiary: #94A3B8;" in content
