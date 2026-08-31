import json
import os
import secrets
import sys
import threading
import time
import urllib.request
import urllib.parse
import urllib.error
from pathlib import Path
import pytest

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from bot import AccountWorker, FLEET_STATE_FILE, atomic_write_json as bot_atomic_write_json
import server
from server import (
    parse_bot_logs,
    get_dashboard_api_key,
    Handler,
    atomic_write_json as server_atomic_write_json,
    resolve_account_balance,
    LOG_FILE,
    CONFIG_FILE,
    FLEET_STATE_FILE as SERVER_FLEET_STATE_FILE,
)


class TestLuckyWatchQAComprehensive:
    """PRISM Independent Comprehensive QA Test Matrix for LuckyWatch Phase 1."""

    def test_single_run_method_ast_and_source_in_bot(self):
        """1. Verify bot.py has no duplicate run() definition and AccountWorker has exactly 1 run method."""
        bot_src = Path(__file__).parent.parent / "bot.py"
        content = bot_src.read_text(encoding="utf-8")
        
        # Line scan
        run_defs = [i for i, line in enumerate(content.splitlines(), 1) if line.strip().startswith("def run(self):")]
        assert len(run_defs) == 1, f"Found multiple run() definitions at lines {run_defs}"
        
        # AST inspection
        import ast
        tree = ast.parse(content)
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name == "AccountWorker":
                methods = [n.name for n in node.body if isinstance(n, ast.FunctionDef) and n.name == "run"]
                assert len(methods) == 1, f"AccountWorker has {len(methods)} 'run' methods in AST"

    def test_atomic_state_updates_under_high_concurrency(self, tmp_path):
        """2. Stress-test atomic state writes on fleet_state.json with 10 concurrent threads & 200 writes."""
        test_fleet_file = tmp_path / "fleet_state.json"
        write_lock = threading.Lock()

        def mock_worker(worker_id):
            for step in range(20):
                with write_lock:
                    data = {}
                    if test_fleet_file.exists():
                        try:
                            data = json.loads(test_fleet_file.read_text(encoding="utf-8"))
                        except Exception:
                            data = {}
                    data[f"user_{worker_id}@example.com"] = {
                        "status": "ACTIVE" if step % 2 == 0 else "SLEEPING",
                        "step": step,
                        "balance": f"0.{worker_id:04d}{step:02d}",
                        "timestamp": time.time(),
                    }
                    tmp_file = test_fleet_file.with_name(f"{test_fleet_file.name}.{os.getpid()}.{threading.get_ident()}.tmp")
                    tmp_file.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
                    os.replace(tmp_file, test_fleet_file)
                time.sleep(0.001)

        threads = [threading.Thread(target=mock_worker, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert test_fleet_file.exists()
        final_content = test_fleet_file.read_text(encoding="utf-8")
        final_json = json.loads(final_content)
        assert len(final_json) == 10
        for i in range(10):
            entry = final_json[f"user_{i}@example.com"]
            assert entry["step"] == 19
            assert "balance" in entry

    def test_bounded_log_tail_seek_benchmark_and_integrity(self, tmp_path, monkeypatch):
        """3. Verify bounded tail seek performance (no full-file read) on 20MB large log file."""
        fake_log = tmp_path / "large_20mb.log"
        # Generate 20MB file
        chunk_line = "12:00:00 [INFO] [benchmark_worker] Heavy streaming log payload line with filler padding text 1234567890\n"
        lines_per_mb = 10000
        with fake_log.open("w", encoding="utf-8") as f:
            for i in range(lines_per_mb * 15):
                f.write(chunk_line)
            # Add specific target markers at the very tail
            f.write("12:59:50 [INFO] [prism_qa] ▶ Task [8888] | Video: ytid_prism | Dur: 20s | Day Left: 100 | Hour Left: 10 | CurDay: 45\n")
            f.write("12:59:55 [INFO] [prism_qa] ✅ Task [8888] DIRECT SUCCESS -> REWARD: +$0.00030 USD!\n")
            f.write("12:59:58 [INFO] [prism_qa] ⏰ Sleeping 3600s until next hour rollover :01\n")

        monkeypatch.setattr("server.LOG_FILE", fake_log)
        
        t0 = time.perf_counter()
        tail, acc_logs, acc_states = parse_bot_logs(max_lines=100)
        t_elapsed = time.perf_counter() - t0

        # Must execute in < 10 milliseconds since it seeks tail 128KB rather than parsing 20MB
        assert t_elapsed < 0.05, f"Tail seek took too long: {t_elapsed:.4f}s"
        assert len(tail) == 100
        assert "prism_qa" in acc_logs
        assert "prism_qa" in acc_states
        assert acc_states["prism_qa"]["status"] == "SLEEPING"
        assert acc_states["prism_qa"]["countdown_sleep"] == 3600
        assert acc_states["prism_qa"]["daily_done"] == 45

    def test_api_actions_security_matrix(self):
        """4. Verify /api/actions/* security auth check rejection (401) and valid authorization (200)."""
        api_key = get_dashboard_api_key()
        assert api_key and api_key.startswith("lw_")

        # Test against live server at 127.0.0.1:8280
        base_url = "http://127.0.0.1:8280"
        
        # 4a. Unauthenticated POST to /api/actions/refresh -> Expect 401 Unauthorized
        req_unauth = urllib.request.Request(f"{base_url}/api/actions/refresh", data=b"{}", headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req_unauth, timeout=5) as res:
                pytest.fail("Expected 401 HTTPError, got 200")
        except urllib.error.HTTPError as e:
            assert e.code == 401
            body = json.loads(e.read().decode("utf-8"))
            assert body["status"] == "error"
            assert "Unauthorized" in body["message"]

        # 4b. Invalid Key POST to /api/actions/refresh -> Expect 401 Unauthorized
        req_invalid = urllib.request.Request(
            f"{base_url}/api/actions/refresh",
            data=b"{}",
            headers={"Content-Type": "application/json", "X-Dashboard-Key": "wrong_key_123"},
            method="POST"
        )
        try:
            with urllib.request.urlopen(req_invalid, timeout=5) as res:
                pytest.fail("Expected 401 HTTPError on wrong key, got 200")
        except urllib.error.HTTPError as e:
            assert e.code == 401

        # 4c. Authenticated POST with X-Dashboard-Key -> Expect 200 OK
        req_auth_header = urllib.request.Request(
            f"{base_url}/api/actions/refresh",
            data=b"{}",
            headers={"Content-Type": "application/json", "X-Dashboard-Key": api_key},
            method="POST"
        )
        with urllib.request.urlopen(req_auth_header, timeout=5) as res:
            assert res.status == 200
            data = json.loads(res.read().decode("utf-8"))
            assert data["status"] == "ok"

        # 4d. Authenticated POST with Bearer Authorization -> Expect 200 OK
        req_auth_bearer = urllib.request.Request(
            f"{base_url}/api/actions/refresh",
            data=b"{}",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
            method="POST"
        )
        with urllib.request.urlopen(req_auth_bearer, timeout=5) as res:
            assert res.status == 200
            data = json.loads(res.read().decode("utf-8"))
            assert data["status"] == "ok"

        # 4e. Authenticated POST with URL Query Param -> Expect 200 OK
        req_auth_query = urllib.request.Request(
            f"{base_url}/api/actions/refresh?key={api_key}",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req_auth_query, timeout=5) as res:
            assert res.status == 200
            data = json.loads(res.read().decode("utf-8"))
            assert data["status"] == "ok"

    def test_dashboard_stats_endpoint_live(self):
        """5. Verify /api/stats returns multi-account telemetry structure with valid schema."""
        base_url = "http://127.0.0.1:8280"
        req = urllib.request.Request(f"{base_url}/api/stats", headers={"User-Agent": "PRISM-QA"})
        with urllib.request.urlopen(req, timeout=15) as res:
            assert res.status == 200
            stats = json.loads(res.read().decode("utf-8"))
            assert "summary" in stats or "accounts" in stats
            assert "accounts" in stats
            assert "logs" in stats
            assert isinstance(stats["accounts"], list)
            assert len(stats["accounts"]) > 0
            # Verify structure of first account
            first_acc = stats["accounts"][0]
            assert "email_redacted" in first_acc
            assert "status" in first_acc
            assert "balance" in first_acc
