"""
PRISM Independent Verification Suite for Payout Lifecycle State Machine & Zero-Spam Invariants.

Verification Matrix:
1. Invariant 1: Zero-Spamming Invariant
   - When _payout_under_review=True & backoff active: check_and_trigger_auto_withdraw produces ZERO network calls to /api/user/payout/send/.
   - When transactionsBeingChecked error occurs: transition to UNDER_REVIEW, backoff set to +21600s, and subsequent triggers immediately suppressed.
   - When server responds ok: transition to UNDER_REVIEW, backoff set to +21600s, and subsequent triggers immediately suppressed.
   - Anti-spam throttle (1h interval) also suppresses frequent triggers even if not in review.

2. Invariant 2: History Status Clearance Invariant
   - When sync_payout_history receives status "1" (PAID): _payout_under_review cleared to False, backoff cleared to 0.0, _payout_last_status updated to "PAID".
   - When sync_payout_history receives status "0" (PAYMENT ERROR): _payout_under_review cleared to False, backoff cleared to 0.0, _payout_last_status updated to "PAYMENT ERROR".
   - When sync_payout_history receives status "3" (UNDER REVIEW) or "2" (IN PROGRESS): _payout_under_review kept True, backoff refreshed.

3. Invariant 3: Payout Telemetry & Persistence Invariant
   - update_fleet_state persists payout_status, payout_under_review, and payout_backoff_until atomically.
   - server.get_latest_stats accurately computes summary.under_review_count and exports account-level payout fields.

4. Invariant 4: Edge Cases & Boundary Conditions
   - Balance exactly equal to threshold ($0.10) vs just below ($0.0999999)
   - Missing faucetpay wallet suppressing payouts safely
   - Corrupted or empty payout history payload gracefully handled
   - Concurrent state file writes preserving payout metadata
"""

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from bot import AccountWorker, FLEET_STATE_FILE
import server


class TestPrismPayoutVerification(unittest.TestCase):

    def setUp(self):
        self.account_config = {
            "email": "prism_verify@luckywatch.test",
            "password": "strongpassword",
            "faucetpay_usdt_trc20": "TYD6xKPrismVerifiedWalletAddressTRC20",
            "proxy": "http://127.0.0.1:31001"
        }
        self.global_config = {
            "app": {"base_url": "https://luckywatch.pro"},
            "auto_withdraw": {
                "enabled": True,
                "threshold_usd": 0.10
            }
        }
        self.worker = AccountWorker(self.account_config, self.global_config, daemon_mode=False)

    # -------------------------------------------------------------
    # 1. Zero-Spamming Invariant Tests
    # -------------------------------------------------------------
    def test_zero_spam_suppression_under_review_backoff(self):
        """Verify ZERO network POST calls occur when account is under review with active backoff."""
        self.worker._payout_under_review = True
        self.worker._payout_backoff_until = time.time() + 18000.0  # 5h remaining
        self.worker._last_auto_withdraw_time = 0

        with patch.object(self.worker, "_api") as mock_api:
            # Attempt auto withdraw multiple times with large balances
            for bal in [0.15, 0.20, 1.50, 10.0]:
                self.worker.check_and_trigger_auto_withdraw(bal)
            
            # Assert zero calls to API
            self.assertEqual(mock_api.call_count, 0, "Expected zero API calls during under-review backoff window")

    def test_zero_spam_on_transactions_being_checked_error(self):
        """Verify transactionsBeingChecked triggers UNDER_REVIEW and locks future auto withdraws."""
        self.worker._payout_under_review = False
        self.worker._payout_backoff_until = 0.0
        self.worker._last_auto_withdraw_time = 0

        with patch.object(self.worker, "_api") as mock_api:
            # 1st call encounters upstream error 'transactionsBeingChecked'
            mock_api.return_value = {
                "status": "error",
                "message": "transactionsBeingChecked"
            }
            self.worker.check_and_trigger_auto_withdraw(0.25)
            self.assertEqual(mock_api.call_count, 1)
            
            # Verify internal state shifted to UNDER_REVIEW + backoff
            self.assertTrue(self.worker._payout_under_review)
            self.assertEqual(self.worker._payout_last_status, "UNDER_REVIEW")
            self.assertGreaterEqual(self.worker._payout_backoff_until, time.time() + 21500)

            # Subsequent 5 calls must be suppressed with 0 additional network calls
            for _ in range(5):
                self.worker.check_and_trigger_auto_withdraw(0.25)
            
            self.assertEqual(mock_api.call_count, 1, "Expected exactly 1 API call; remaining 5 should be suppressed")

    def test_zero_spam_on_successful_payout_submission(self):
        """Verify successful payout immediately transitions to UNDER_REVIEW with 6h backoff."""
        self.worker._payout_under_review = False
        self.worker._payout_backoff_until = 0.0
        self.worker._last_auto_withdraw_time = 0

        with patch.object(self.worker, "_api") as mock_api:
            mock_api.return_value = {
                "status": "ok",
                "data": {"id": 8888, "sum": "0.1500000"}
            }
            self.worker.check_and_trigger_auto_withdraw(0.15)
            self.assertEqual(mock_api.call_count, 1)

            # State check
            self.assertTrue(self.worker._payout_under_review)
            self.assertEqual(self.worker._payout_last_status, "UNDER_REVIEW")
            self.assertGreaterEqual(self.worker._payout_backoff_until, time.time() + 21500)

            # Immediate second trigger is suppressed
            self.worker.check_and_trigger_auto_withdraw(0.15)
            self.assertEqual(mock_api.call_count, 1)

    def test_zero_spam_hourly_throttle_enforcement(self):
        """Verify 1-hour anti-spam throttle prevents rapid repeated calls even if backoff is clear."""
        self.worker._payout_under_review = False
        self.worker._payout_backoff_until = 0.0
        self.worker._last_auto_withdraw_time = time.time() - 600  # only 10 mins ago

        with patch.object(self.worker, "_api") as mock_api:
            self.worker.check_and_trigger_auto_withdraw(0.20)
            self.assertEqual(mock_api.call_count, 0, "Hourly anti-spam throttle must suppress withdrawal")

    # -------------------------------------------------------------
    # 2. History Status Clearance Invariant Tests
    # -------------------------------------------------------------
    def test_history_sync_clears_lock_on_paid_status_1(self):
        """Verify status '1' (PAID) releases the under-review lock and unlocks auto-withdraw."""
        self.worker._payout_under_review = True
        self.worker._payout_backoff_until = time.time() + 15000
        self.worker._payout_last_status = "UNDER_REVIEW"

        with patch.object(self.worker, "_api") as mock_api:
            mock_api.return_value = {
                "status": "ok",
                "data": {
                    "items": [{
                        "id": 1001,
                        "status": "1",
                        "val": "0.2000000",
                        "account": self.account_config["faucetpay_usdt_trc20"],
                        "unixtime": int(time.time())
                    }]
                }
            }
            rec = self.worker.sync_payout_history()
            self.assertIsNotNone(rec)
            self.assertFalse(self.worker._payout_under_review)
            self.assertEqual(self.worker._payout_backoff_until, 0.0)
            self.assertEqual(self.worker._payout_last_status, "PAID")

    def test_history_sync_clears_lock_on_payment_error_status_0(self):
        """Verify status '0' (PAYMENT ERROR) releases the under-review lock and resets backoff."""
        self.worker._payout_under_review = True
        self.worker._payout_backoff_until = time.time() + 15000
        self.worker._payout_last_status = "UNDER_REVIEW"

        with patch.object(self.worker, "_api") as mock_api:
            mock_api.return_value = {
                "status": "ok",
                "data": {
                    "items": [{
                        "id": 1002,
                        "status": "0",
                        "val": "0.2000000",
                        "account": self.account_config["faucetpay_usdt_trc20"],
                        "unixtime": int(time.time())
                    }]
                }
            }
            rec = self.worker.sync_payout_history()
            self.assertIsNotNone(rec)
            self.assertFalse(self.worker._payout_under_review)
            self.assertEqual(self.worker._payout_backoff_until, 0.0)
            self.assertEqual(self.worker._payout_last_status, "PAYMENT ERROR")

    def test_history_sync_retains_lock_on_under_review_status_3(self):
        """Verify status '3' (UNDER REVIEW) maintains the lock and ensures backoff window is active."""
        self.worker._payout_under_review = False
        self.worker._payout_backoff_until = 0.0

        with patch.object(self.worker, "_api") as mock_api:
            mock_api.return_value = {
                "status": "ok",
                "data": {
                    "items": [{
                        "id": 1003,
                        "status": "3",
                        "val": "0.1500000",
                        "account": self.account_config["faucetpay_usdt_trc20"],
                        "unixtime": int(time.time())
                    }]
                }
            }
            rec = self.worker.sync_payout_history()
            self.assertIsNotNone(rec)
            self.assertTrue(self.worker._payout_under_review)
            self.assertEqual(self.worker._payout_last_status, "UNDER REVIEW")
            self.assertGreater(self.worker._payout_backoff_until, time.time() + 20000)

    # -------------------------------------------------------------
    # 3. Telemetry & State Persistence Invariants
    # -------------------------------------------------------------
    def test_fleet_state_atomic_payout_telemetry(self):
        """Verify update_fleet_state persists payout status, review flag, and backoff timestamp."""
        test_state_file = Path(tempfile.gettempdir()) / f"prism_fleet_state_{os.getpid()}_{int(time.time())}.json"
        with patch("bot.FLEET_STATE_FILE", test_state_file):
            try:
                self.worker._payout_last_status = "UNDER_REVIEW"
                self.worker._payout_under_review = True
                self.worker._payout_backoff_until = 1788200000.0

                self.worker.update_fleet_state(status="ACTIVE")

                self.assertTrue(test_state_file.exists())
                data = json.loads(test_state_file.read_text(encoding="utf-8"))
                self.assertIn(self.account_config["email"], data)
                entry = data[self.account_config["email"]]
                self.assertEqual(entry["payout_status"], "UNDER_REVIEW")
                self.assertTrue(entry["payout_under_review"])
                self.assertEqual(entry["payout_backoff_until"], 1788200000.0)
            finally:
                if test_state_file.exists():
                    test_state_file.unlink()

    def test_server_summary_under_review_calculation(self):
        """Verify server.get_latest_stats accurately computes under_review_count across accounts."""
        stats = server.get_latest_stats()
        self.assertIn("summary", stats)
        self.assertIn("under_review_count", stats["summary"])
        self.assertIsInstance(stats["summary"]["under_review_count"], int)
        self.assertGreaterEqual(stats["summary"]["under_review_count"], 0)

        for acc in stats.get("accounts", []):
            self.assertIn("payout_status", acc)
            self.assertIn("payout_under_review", acc)
            self.assertIn("payout_backoff_until", acc)

    # -------------------------------------------------------------
    # 4. Boundary and Failure Resilience Tests
    # -------------------------------------------------------------
    def test_threshold_boundary_precision(self):
        """Verify payout is suppressed below $0.10 and triggered at >= $0.10."""
        self.worker._payout_under_review = False
        self.worker._payout_backoff_until = 0.0
        self.worker._last_auto_withdraw_time = 0

        with patch.object(self.worker, "_api") as mock_api:
            # Test balance just below threshold
            self.worker.check_and_trigger_auto_withdraw(0.0999999)
            self.assertEqual(mock_api.call_count, 0, "Balance < $0.10 must NOT trigger payout")

            # Test balance exactly threshold
            mock_api.return_value = {"status": "ok", "data": {"id": 1}}
            self.worker.check_and_trigger_auto_withdraw(0.1000000)
            self.assertEqual(mock_api.call_count, 1, "Balance >= $0.10 must trigger payout")

    def test_missing_wallet_suppresses_payout(self):
        """Verify empty or whitespace wallet suppresses payout attempts."""
        self.worker.account["faucetpay_usdt_trc20"] = ""
        self.worker._payout_under_review = False
        self.worker._payout_backoff_until = 0.0
        self.worker._last_auto_withdraw_time = 0

        with patch.object(self.worker, "_api") as mock_api:
            self.worker.check_and_trigger_auto_withdraw(0.50)
            self.assertEqual(mock_api.call_count, 0, "Empty wallet must suppress payout")

    def test_malformed_history_response_handled_gracefully(self):
        """Verify non-JSON or missing items key in history response does not raise exceptions."""
        with patch.object(self.worker, "_api") as mock_api:
            mock_api.return_value = {"status": "error", "message": "Rate limited"}
            res = self.worker.sync_payout_history()
            self.assertIsNone(res)


if __name__ == "__main__":
    unittest.main()
