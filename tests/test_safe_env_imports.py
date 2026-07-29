import os
import subprocess
import sys


def test_blank_new_float_env_values_do_not_crash_module_import():
    env = os.environ.copy()
    env["BALANCE_SNAPSHOT_TTL_SEC"] = ""
    env["F3_FAST_RECHECK_MAX_AGE_SEC"] = ""
    env["F4_WS_STALE_SEC"] = ""
    env["F4_REST_POLL_INTERVAL_SEC"] = ""
    env["F4_FILL_POLL_INTERVAL_SEC"] = ""
    env["F4_STATE_PERSIST_INTERVAL_SEC"] = ""
    env["F4_HEARTBEAT_INTERVAL_SEC"] = ""
    env["PAPER_FAST_PROBE_OPEN_TIMEOUT_SEC"] = ""

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import main;"
                "from src.modules import f3_entry, f4_tracking;"
                "assert f3_entry.BALANCE_SNAPSHOT_TTL_SEC == 90.0;"
                "assert f3_entry.F3_FAST_RECHECK_MAX_AGE_SEC == 15.0;"
                "assert f4_tracking.F4_WS_STALE_SEC == 2.0;"
                "assert f4_tracking.F4_REST_POLL_INTERVAL_SEC == 1.0;"
                "assert f4_tracking.F4_FILL_POLL_INTERVAL_SEC == 0.5;"
                "assert f4_tracking.F4_STATE_PERSIST_INTERVAL_SEC == 1.0;"
                "assert f4_tracking.F4_HEARTBEAT_INTERVAL_SEC == 30.0;"
                "assert main.PAPER_FAST_PROBE_OPEN_TIMEOUT_SEC == 2.5"
            ),
        ],
        cwd=os.getcwd(),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_negative_f4_intervals_are_clamped_at_zero():
    env = os.environ.copy()
    env["F4_WS_STALE_SEC"] = "-2"
    env["F4_REST_POLL_INTERVAL_SEC"] = "-5"
    env["F4_FILL_POLL_INTERVAL_SEC"] = "-0.5"
    env["F4_STATE_PERSIST_INTERVAL_SEC"] = "-1"

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from src.modules import f4_tracking;"
                "assert f4_tracking.F4_WS_STALE_SEC == 0.0;"
                "assert f4_tracking.F4_REST_POLL_INTERVAL_SEC == 0.0;"
                "assert f4_tracking.F4_FILL_POLL_INTERVAL_SEC == 0.0;"
                "assert f4_tracking.F4_STATE_PERSIST_INTERVAL_SEC == 0.0"
            ),
        ],
        cwd=os.getcwd(),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
