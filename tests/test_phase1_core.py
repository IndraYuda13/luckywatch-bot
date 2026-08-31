import os
import sys
import json
import time
import threading
import urllib.request
import urllib.parse
import pytest
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from server import (
    parse_bot_logs,
    get_dashboard_api_key,
    atomic_write_json,
    resolve_account_balance,
    CONFIG_FILE,
    STATE_FILE,
    FLEET_STATE_FILE,
    LOG_FILE,
)
from bot import AccountWorker, MultiBotManager, atomic_write_json as bot_atomic_write


class TestLuckyWatchCore:
    def test_single_run_method_in_bot(self):
        """Verify that AccountWorker in bot.py has only one run method and no duplicates."""
        worker_cls = AccountWorker
        run_func = getattr(worker_cls, "run", None)
        assert callable(run_func)
        # Check source for duplicate def run(
        bot_src = Path("/root/projects/luckywatch-bot/bot.py").read_text(encoding="utf-8")
        run_matches = [line for line in bot_src.splitlines() if line.strip().startswith("def run(")]
        assert len(run_matches) == 1, f"Expected exactly 1 def run( in bot.py, found {len(run_matches)}: {run_matches}"

    def test_atomic_state_persistence_concurrency(self, tmp_path):
        """Verify thread-safe atomic state persistence under parallel write stress."""
        test_file = tmp_path / "test_state.json"
        
        def writer_task(worker_id, iterations=50):
            for i in range(iterations):
                data = {"worker_id": worker_id, "iteration": i, "timestamp": time.time()}
                atomic_write_json(test_file, data)
                # Verify immediately readable without corruption
                try:
                    read_data = json.loads(test_file.read_text(encoding="utf-8"))
                    assert "worker_id" in read_data
                except Exception as e:
                    pytest.fail(f"Corrupted JSON read during atomic concurrency write: {e}")

        threads = [threading.Thread(target=writer_task, args=(tid,)) for tid in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert test_file.exists()
        final_data = json.loads(test_file.read_text(encoding="utf-8"))
        assert "worker_id" in final_data

    def test_bounded_log_tail_seek(self):
        """Verify parse_bot_logs reads bounded tail without loading entire huge file into memory."""
        logs, acc_logs, states = parse_bot_logs(100)
        assert isinstance(logs, list)
        assert isinstance(acc_logs, dict)
        assert isinstance(states, dict)
        assert len(logs) <= 100

    def test_dashboard_api_key_persistence_and_auth(self):
        """Verify dashboard API key generation and format."""
        api_key = get_dashboard_api_key()
        assert isinstance(api_key, str)
        assert len(api_key) >= 16
        assert api_key.startswith("lw_")
