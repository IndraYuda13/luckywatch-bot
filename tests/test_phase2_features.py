#!/usr/bin/env python3
"""
Unit and Integration Tests for LuckyWatch Phase 2:
- Android Chrome UA & Client Hints Harmonization
- Staggered Hourly & Daily Wake-Up Jitter (Anti-Thundering-Herd)
- Smart Daily Bonus End-of-Day (EOD) Milestone Fallback
- Behavioral Timing & Captcha Coordinate Jitter (+-1 to +-2 px within bounds)
"""

import json
import random
import unittest
from unittest.mock import MagicMock, patch
from pathlib import Path

from bot import AccountWorker


class TestLuckyWatchPhase2(unittest.TestCase):
    def setUp(self):
        self.global_config = {
            "accounts": [
                {"email": "worker0@gmail.com", "password": "pass", "proxy": "http://127.0.0.1:31001", "active": True},
                {"email": "worker1@gmail.com", "password": "pass", "proxy": "http://127.0.0.1:31002", "active": True},
                {"email": "worker2@gmail.com", "password": "pass", "proxy": "http://127.0.0.1:31003", "active": True},
                {"email": "worker3@gmail.com", "password": "pass", "proxy": "http://127.0.0.1:31004", "active": True},
                {"email": "worker4@gmail.com", "password": "pass", "proxy": "http://127.0.0.1:31005", "active": True},
            ],
            "app": {
                "base_url": "https://luckywatch.pro",
                "signin_url": "https://luckywatch.pro/signin",
                "watch_url": "https://luckywatch.pro/watch",
            },
            "runner": {
                "delay_between_videos_seconds": 2,
                "auto_daily_bonus": True,
            }
        }
        self.worker0 = AccountWorker(self.global_config["accounts"][0], self.global_config, daemon_mode=False)
        self.worker1 = AccountWorker(self.global_config["accounts"][1], self.global_config, daemon_mode=False)
        self.worker4 = AccountWorker(self.global_config["accounts"][4], self.global_config, daemon_mode=False)

    def test_android_chrome_ua_and_client_hints(self):
        """1. Verify Android Chrome UA and Client Hints headers."""
        headers = self.worker0._get_default_headers(referer_path="/watch")
        
        # Check User-Agent format
        self.assertIn("Android 14", self.worker0.user_agent)
        self.assertIn("Chrome/124", self.worker0.user_agent)
        self.assertEqual(headers["User-Agent"], self.worker0.user_agent)
        
        # Check standard Client Hints headers
        self.assertEqual(headers["Sec-CH-UA-Mobile"], "?1")
        self.assertEqual(headers["Sec-CH-UA-Platform"], '"Android"')
        self.assertIn("Chromium", headers["Sec-CH-UA"])
        self.assertEqual(headers["Sec-Fetch-Site"], "same-origin")
        self.assertEqual(headers["Sec-Fetch-Mode"], "cors")
        self.assertEqual(headers["Sec-Fetch-Dest"], "empty")
        self.assertIn("en-US", headers["Accept-Language"])

    def test_staggered_hourly_wakeup_jitter_distribution(self):
        """2. Verify staggered wake-up jitter distributes restart offsets across accounts."""
        sleep_w0 = self.worker0.seconds_until_next_hour()
        sleep_w1 = self.worker1.seconds_until_next_hour()
        sleep_w4 = self.worker4.seconds_until_next_hour()
        
        # All sleep times must be valid positive values
        self.assertGreaterEqual(sleep_w0, 5)
        self.assertGreaterEqual(sleep_w1, 5)
        self.assertGreaterEqual(sleep_w4, 5)
        
        # Worker 4 (index 4 * 8s = 32s base offset) should have significantly larger offset than worker 0 (index 0)
        diff_4_0 = sleep_w4 - sleep_w0
        self.assertGreaterEqual(diff_4_0, 20, f"Worker 4 vs Worker 0 stagger difference too small: {diff_4_0}s")

    def test_daily_bonus_normal_wait_before_500(self):
        """3a. Verify Daily Bonus postpones claim during mid-day if viewCurDay < 500."""
        # 12 hours left in day, 250 views completed
        mock_info = {
            "status": "ok",
            "data": {
                "dailyBonusCnt": 0,
                "viewCurDay": 250,
                "secondsUntilEndOfDay": 43200
            }
        }
        with patch.object(self.worker0, "_api", return_value=mock_info):
            res = self.worker0.claim_daily_bonus()
            self.assertEqual(res["status"], "in_progress")
            self.assertEqual(res["viewCurDay"], 250)
            self.assertEqual(res["needed"], 250)

    def test_daily_bonus_max_tier_claim_500(self):
        """3b. Verify Daily Bonus claims top tier ($0.010) immediately when viewCurDay >= 500."""
        mock_info = {
            "status": "ok",
            "data": {
                "dailyBonusCnt": 0,
                "viewCurDay": 500,
                "secondsUntilEndOfDay": 36000
            }
        }
        mock_claim = {"status": "ok", "data": {"prize": "$0.010 USD"}}
        
        def api_side_effect(endpoint, data=None, timeout=15):
            if data and data.get("method") == "getInfo":
                return mock_info
            elif data and data.get("method") == "getBonus":
                return mock_claim
            return {"status": "error"}

        with patch.object(self.worker0, "_api", side_effect=api_side_effect):
            res = self.worker0.claim_daily_bonus()
            self.assertEqual(res["status"], "ok")
            self.assertEqual(res["data"]["prize"], "$0.010 USD")

    def test_daily_bonus_eod_fallback_tier_400(self):
        """3c. Verify Daily Bonus claims tier 400 when secondsUntilEndOfDay <= 1800 and viewCurDay == 420."""
        mock_info = {
            "status": "ok",
            "data": {
                "dailyBonusCnt": 0,
                "viewCurDay": 420,
                "secondsUntilEndOfDay": 1200  # 20 mins before midnight
            }
        }
        mock_claim = {"status": "ok", "data": {"prize": "$0.005 USD (Tier 400)"}}
        
        calls = []
        def api_side_effect(endpoint, data=None, timeout=15):
            calls.append((endpoint, data))
            if data and data.get("method") == "getInfo":
                return mock_info
            elif data and data.get("method") == "getBonus":
                return mock_claim
            return {"status": "error"}

        with patch.object(self.worker0, "_api", side_effect=api_side_effect):
            res = self.worker0.claim_daily_bonus()
            self.assertEqual(res["status"], "ok")
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[1][1]["method"], "getBonus")

    def test_daily_bonus_eod_fallback_tier_100(self):
        """3d. Verify Daily Bonus claims tier 100 when secondsUntilEndOfDay <= 1800 and viewCurDay == 150."""
        mock_info = {
            "status": "ok",
            "data": {
                "dailyBonusCnt": 0,
                "viewCurDay": 150,
                "secondsUntilEndOfDay": 600  # 10 mins before midnight
            }
        }
        mock_claim = {"status": "ok", "data": {"prize": "100 clovers (Tier 100)"}}

        def api_side_effect(endpoint, data=None, timeout=15):
            if data and data.get("method") == "getInfo":
                return mock_info
            elif data and data.get("method") == "getBonus":
                return mock_claim
            return {"status": "error"}

        with patch.object(self.worker0, "_api", side_effect=api_side_effect):
            res = self.worker0.claim_daily_bonus()
            self.assertEqual(res["status"], "ok")

    def test_daily_bonus_eod_below_minimum_views(self):
        """3e. Verify Daily Bonus does not claim if viewCurDay < 100 at EOD."""
        mock_info = {
            "status": "ok",
            "data": {
                "dailyBonusCnt": 0,
                "viewCurDay": 75,
                "secondsUntilEndOfDay": 900
            }
        }
        with patch.object(self.worker0, "_api", return_value=mock_info):
            res = self.worker0.claim_daily_bonus()
            self.assertEqual(res["status"], "below_min_threshold")

    def test_submit_captcha_coordinates_jitter_and_bounds(self):
        """4. Verify captcha coordinate submission adds subtle jitter within bounds."""
        base_clicks = [
            {"x": 100, "y": 150},
            {"x": 200, "y": 250},
            {"x": 50, "y": 80}
        ]
        
        captured_payloads = []
        def mock_api(endpoint, data=None, timeout=15):
            captured_payloads.append(data)
            return {"status": "ok", "data": {"reward": "0.0002500"}}

        with patch.object(self.worker0, "_api", side_effect=mock_api):
            for _ in range(20):
                self.worker0.submit_captcha_coordinates(base_clicks)

        self.assertEqual(len(captured_payloads), 20)
        x0_values = [int(p["coor[0][x]"]) for p in captured_payloads]
        
        # Verify coordinates stayed within reasonable jitter range (+-2 px)
        for x in x0_values:
            self.assertTrue(98 <= x <= 102, f"Coordinate x {x} out of expected jitter range [98, 102]")
        
        # Verify that jitter actually produces variation across 20 trials
        self.assertGreater(len(set(x0_values)), 1, "Jitter should produce non-deterministic variations")


if __name__ == "__main__":
    unittest.main()
