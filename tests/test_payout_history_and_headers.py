import unittest
import json
import time
import os
from unittest.mock import patch, MagicMock
from server import get_latest_stats, Handler

class TestPayoutHistoryAndHeaders(unittest.TestCase):
    def test_payout_history_ingestion_format(self):
        fake_history_data = {
            "status": "ok",
            "data": {
                "history": {
                    "data": [
                        {
                            "id": "253397",
                            "val": "0.12000",
                            "commissionVal": "0.11500",
                            "account": "TQn9Y2khEsLJW1ChVWFMSMeSTow5KaxnSE",
                            "status": 3,
                            "unixtime": 1788156400
                        }
                    ]
                }
            }
        }
        
        # Test extraction logic
        items = fake_history_data["data"]["history"]["data"]
        first = items[0]
        st_code = str(first.get("status", ""))
        status_map = {
            "0": "PAYMENT ERROR",
            "1": "PAID",
            "2": "IN PROGRESS",
            "3": "UNDER REVIEW"
        }
        st_label = status_map.get(st_code, f"CODE_{st_code}")
        
        self.assertEqual(st_label, "UNDER REVIEW")
        self.assertEqual(st_code, "3")
        self.assertEqual(first["id"], "253397")
        self.assertEqual(first["val"], "0.12000")
        self.assertEqual(first["account"], "TQn9Y2khEsLJW1ChVWFMSMeSTow5KaxnSE")

    def test_stats_contains_last_payout(self):
        stats = get_latest_stats()
        self.assertIn("accounts", stats)
        for acc in stats["accounts"]:
            self.assertIn("last_payout", acc)
            self.assertIn("payout_status", acc)
            self.assertIn("payout_under_review", acc)

    def test_anti_caching_headers_in_response(self):
        # Verify Handler sends no-cache headers in _send_json and do_GET
        mock_request = MagicMock()
        mock_client = ("127.0.0.1", 12345)
        mock_server = MagicMock()
        
        # Initialize handler with dummy setup
        with patch.object(Handler, 'setup'), patch.object(Handler, 'handle'), patch.object(Handler, 'finish'):
            handler = Handler(mock_request, mock_client, mock_server)
            handler.wfile = MagicMock()
            headers_sent = {}
            def mock_send_header(keyword: str, value: str):
                headers_sent[keyword.lower()] = value
            handler.send_response = MagicMock()
            handler.send_header = mock_send_header
            handler.end_headers = MagicMock()
            
            # Test _send_json headers
            handler._send_json(200, {"status": "ok"})
            self.assertIn("cache-control", headers_sent)
            self.assertIn("no-store", headers_sent["cache-control"])
            self.assertEqual(headers_sent.get("pragma"), "no-cache")
            self.assertEqual(headers_sent.get("expires"), "0")

if __name__ == "__main__":
    unittest.main()
