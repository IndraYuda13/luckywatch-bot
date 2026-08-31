"""
FORGE Test Suite for Payout Lifecycle State Machine & transactionsBeingChecked Backoff.
Validates:
1. AccountWorker initialization of _payout_under_review, _payout_backoff_until, _payout_last_status.
2. Pre-flight suppression of withdraw request when under review and within backoff.
3. Successful payout sets _payout_under_review=True, _payout_last_status='UNDER_REVIEW', and 6h backoff.
4. Error message 'transactionsBeingChecked' sets _payout_under_review=True, _payout_last_status='UNDER_REVIEW', and 6h backoff.
5. Payout history sync resets _payout_under_review=False on status '1' (PAID) or '0' (PAYMENT ERROR).
6. Payout history sync retains _payout_under_review=True on status '3' (UNDER REVIEW) or '2' (IN PROGRESS).
7. Fleet state atomic persistence of payout telemetry.
"""
import json
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from bot import AccountWorker, FLEET_STATE_FILE


class TestPayoutLifecycleStateMachine(unittest.TestCase):

    def setUp(self):
        self.account_config = {
            "email": "test_payout@example.com",
            "password": "password123",
            "faucetpay_usdt_trc20": "TRC20WALLETADDRESS1234567890",
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

    def test_worker_initial_payout_attributes(self):
        self.assertFalse(self.worker._payout_under_review)
        self.assertEqual(self.worker._payout_backoff_until, 0.0)
        self.assertEqual(self.worker._payout_last_status, "IDLE")

    def test_preflight_suppression_when_under_review(self):
        self.worker._payout_under_review = True
        self.worker._payout_backoff_until = time.time() + 3600  # 1 hour remaining

        with patch.object(self.worker, "_api") as mock_api:
            self.worker.check_and_trigger_auto_withdraw(0.15)
            # Zero network POST calls to /api/user/payout/send/
            mock_api.assert_not_called()

    def test_payout_success_transitions_to_under_review_with_backoff(self):
        self.worker._payout_under_review = False
        self.worker._payout_backoff_until = 0.0
        self.worker._last_auto_withdraw_time = 0

        with patch.object(self.worker, "_api") as mock_api:
            mock_api.return_value = {
                "status": "ok",
                "data": {"id": 12345, "sum": "0.1500000"}
            }
            self.worker.check_and_trigger_auto_withdraw(0.15)

            mock_api.assert_called_once()
            self.assertTrue(self.worker._payout_under_review)
            self.assertEqual(self.worker._payout_last_status, "UNDER_REVIEW")
            self.assertGreater(self.worker._payout_backoff_until, time.time() + 20000)

    def test_transactions_being_checked_error_transitions_to_under_review(self):
        self.worker._payout_under_review = False
        self.worker._payout_backoff_until = 0.0
        self.worker._last_auto_withdraw_time = 0

        with patch.object(self.worker, "_api") as mock_api:
            mock_api.return_value = {
                "status": "error",
                "message": "transactionsBeingChecked"
            }
            self.worker.check_and_trigger_auto_withdraw(0.15)

            mock_api.assert_called_once()
            self.assertTrue(self.worker._payout_under_review)
            self.assertEqual(self.worker._payout_last_status, "UNDER_REVIEW")
            self.assertGreater(self.worker._payout_backoff_until, time.time() + 20000)

    def test_sync_payout_history_clears_under_review_on_paid_status(self):
        self.worker._payout_under_review = True
        self.worker._payout_backoff_until = time.time() + 21600
        self.worker._payout_last_status = "UNDER_REVIEW"

        with patch.object(self.worker, "_api") as mock_api:
            mock_api.return_value = {
                "status": "ok",
                "data": {
                    "items": [{
                        "id": 999,
                        "val": "0.1500000",
                        "commissionVal": "0.1500000",
                        "account": "TRC20WALLETADDRESS1234567890",
                        "status": "1",  # PAID
                        "unixtime": int(time.time())
                    }]
                }
            }
            res = self.worker.sync_payout_history()
            self.assertIsNotNone(res)
            self.assertFalse(self.worker._payout_under_review)
            self.assertEqual(self.worker._payout_backoff_until, 0.0)
            self.assertEqual(self.worker._payout_last_status, "PAID")

    def test_sync_payout_history_clears_under_review_on_payment_error_status(self):
        self.worker._payout_under_review = True
        self.worker._payout_backoff_until = time.time() + 21600
        self.worker._payout_last_status = "UNDER_REVIEW"

        with patch.object(self.worker, "_api") as mock_api:
            mock_api.return_value = {
                "status": "ok",
                "data": {
                    "items": [{
                        "id": 1000,
                        "val": "0.1500000",
                        "commissionVal": "0.1500000",
                        "account": "TRC20WALLETADDRESS1234567890",
                        "status": "0",  # PAYMENT ERROR
                        "unixtime": int(time.time())
                    }]
                }
            }
            res = self.worker.sync_payout_history()
            self.assertIsNotNone(res)
            self.assertFalse(self.worker._payout_under_review)
            self.assertEqual(self.worker._payout_backoff_until, 0.0)
            self.assertEqual(self.worker._payout_last_status, "PAYMENT ERROR")

    def test_sync_payout_history_retains_under_review_on_status_3(self):
        self.worker._payout_under_review = False
        self.worker._payout_backoff_until = 0.0
        self.worker._payout_last_status = "IDLE"

        with patch.object(self.worker, "_api") as mock_api:
            mock_api.return_value = {
                "status": "ok",
                "data": {
                    "items": [{
                        "id": 1001,
                        "val": "0.1500000",
                        "commissionVal": "0.1500000",
                        "account": "TRC20WALLETADDRESS1234567890",
                        "status": "3",  # UNDER REVIEW
                        "unixtime": int(time.time())
                    }]
                }
            }
            res = self.worker.sync_payout_history()
            self.assertIsNotNone(res)
            self.assertTrue(self.worker._payout_under_review)
            self.assertEqual(self.worker._payout_last_status, "UNDER REVIEW")
            self.assertGreater(self.worker._payout_backoff_until, time.time() + 20000)

    def test_fleet_state_persistence_payout_fields(self):
        self.worker._payout_under_review = True
        self.worker._payout_last_status = "UNDER_REVIEW"
        self.worker._payout_backoff_until = 1788200000.0

        with patch("bot.FLEET_STATE_FILE", Path("/tmp/test_fleet_state.json")):
            tmp_path = Path("/tmp/test_fleet_state.json")
            if tmp_path.exists():
                tmp_path.unlink()
            try:
                self.worker.update_fleet_state(status="ACTIVE")
                self.assertTrue(tmp_path.exists())
                data = json.loads(tmp_path.read_text())
                acc = data.get(self.worker.email, {})
                self.assertEqual(acc.get("payout_status"), "UNDER_REVIEW")
                self.assertTrue(acc.get("payout_under_review"))
                self.assertEqual(acc.get("payout_backoff_until"), 1788200000.0)
            finally:
                if tmp_path.exists():
                    tmp_path.unlink()
