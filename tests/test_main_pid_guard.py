import os

import pytest

import main


def teardown_function():
    main._clear_pid()


def test_write_pid_refuses_when_pid_file_is_locked(tmp_path, monkeypatch):
    msvcrt = pytest.importorskip("msvcrt")
    pid_file = tmp_path / "main.pid"
    monkeypatch.setattr(main, "PID_PATH", str(pid_file))
    pid_file.write_text("12345", encoding="utf-8")
    events = []

    with open(pid_file, "r+", encoding="utf-8") as handle:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        try:
            monkeypatch.setattr(
                main.logger, "log",
                lambda event, **kwargs: events.append((event, kwargs)),
            )

            assert main._write_pid() is False
            handle.seek(0)
            assert handle.read().strip() == "12345"
            assert events[0][0] == "PROCESS_ALREADY_RUNNING"
            assert events[0][1]["current_pid"] == os.getpid()
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def test_write_pid_replaces_unlocked_stale_pid_file(tmp_path, monkeypatch):
    pid_file = tmp_path / "main.pid"
    monkeypatch.setattr(main, "PID_PATH", str(pid_file))
    pid_file.write_text("12345", encoding="utf-8")
    events = []

    monkeypatch.setattr(main.logger, "log", lambda event, **kwargs: events.append((event, kwargs)))

    assert main._write_pid() is True
    main._pid_lock_file.seek(0)
    assert main._pid_lock_file.read().strip() == str(os.getpid())
    assert any(event == "STALE_PID_REPLACED" for event, _ in events)

    main._clear_pid()
    assert pid_file.exists()


def test_clear_pid_releases_lock_and_leaves_reusable_pid_file(tmp_path, monkeypatch):
    pid_file = tmp_path / "main.pid"
    monkeypatch.setattr(main, "PID_PATH", str(pid_file))

    assert main._write_pid() is True
    main._clear_pid()

    assert pid_file.exists()
    assert main._write_pid() is True
    main._clear_pid()
