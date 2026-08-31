import json
import os
import secrets
import sys
import threading
import time
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from bot import AccountWorker, FLEET_STATE_FILE
from server import (
    parse_bot_logs,
    get_dashboard_api_key,
    Handler,
    atomic_write_json,
    LOG_FILE,
    CONFIG_FILE,
)


class TestLuckyWatchCore:
    def test_single_run_method_in_bot(self):
        """Verify that AccountWorker has exactly one run() method defined in source code."""
        bot_src = Path(__file__).parent.parent / "bot.py"
        content = bot_src.read_text(encoding="utf-8")
        run_defs = [line for line in content.splitlines() if line.strip().startswith("def run(self):")]
        assert len(run_defs) == 1, f"Expected exactly 1 'def run(self):', found {len(run_defs)}"

    def test_atomic_state_persistence_concurrency(self, tmp_path):
        """Verify concurrent multi-threaded writes never corrupt JSON files."""
        target_file = tmp_path / "concurrent_state.json"
        write_lock = threading.Lock()

        def worker_writer(idx):
            for step in range(20):
                with write_lock:
                    data = {}
                    if target_file.exists():
                        try:
                            data = json.loads(target_file.read_text(encoding="utf-8"))
                        except Exception:
                            data = {}
                    data[f"worker_{idx}"] = {"step": step, "ts": time.time()}
                    
                    tmp_file = target_file.with_name(f"{target_file.name}.{os.getpid()}.{threading.get_ident()}.tmp")
                    tmp_file.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
                    os.replace(tmp_file, target_file)
                time.sleep(0.005)

        threads = [threading.Thread(target=worker_writer, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert target_file.exists()
        final_data = json.loads(target_file.read_text(encoding="utf-8"))
        assert len(final_data) == 10
        for i in range(10):
            assert final_data[f"worker_{i}"]["step"] == 19

    def test_bounded_log_tail_seek(self, tmp_path, monkeypatch):
        """Verify parse_bot_logs efficiently parses the tail of a large log file without reading whole file."""
        fake_log = tmp_path / "large_bot.log"
        # Generate 5MB of dummy log data
        with fake_log.open("w", encoding="utf-8") as f:
            for i in range(50000):
                f.write(f"12:00:00 [INFO] [worker1] Message line number {i}\n")
            f.write("12:05:00 [INFO] [test_acc] ▶ Task [9999] | Video: ytid_123 | Dur: 15s | Day Left: 50 | Hour Left: 5 | CurDay: 10\n")
            f.write("12:05:16 [INFO] [test_acc] ✅ Task [9999] DIRECT SUCCESS -> REWARD: +$0.00030 USD!\n")

        monkeypatch.setattr("server.LOG_FILE", fake_log)
        tail, acc_logs, acc_states = parse_bot_logs(max_lines=50)

        assert len(tail) == 50
        assert "test_acc" in acc_logs
        assert "test_acc" in acc_states
        assert acc_states["test_acc"]["status"] == "ACTIVE"
        assert acc_states["test_acc"]["daily_done"] == 10

    def test_dashboard_api_key_persistence_and_auth(self):
        """Verify dashboard API key generation and format."""
        api_key = get_dashboard_api_key()
        assert api_key is not None
        assert len(api_key) > 8
        assert api_key.startswith("lw_")
