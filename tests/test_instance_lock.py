"""A second worker or replica on the same DATA_DIR must refuse to start."""

from __future__ import annotations

import subprocess
import sys

import pytest

from infrastructure.instance_lock import InstanceLock, InstanceLockError, check_worker_count


def test_second_lock_on_the_same_data_dir_is_refused(tmp_path):
    first = InstanceLock(tmp_path)
    first.acquire()
    try:
        with pytest.raises(InstanceLockError, match="Another process"):
            InstanceLock(tmp_path).acquire()
    finally:
        first.release()
    again = InstanceLock(tmp_path)
    again.acquire()  # released locks can be taken again (restart, test clients)
    assert again.held
    again.release()


def test_lock_is_held_against_another_process(tmp_path):
    lock = InstanceLock(tmp_path)
    lock.acquire()
    try:
        code = (
            "import sys; from infrastructure.instance_lock import InstanceLock, InstanceLockError\n"
            "try:\n    InstanceLock(sys.argv[1]).acquire()\nexcept InstanceLockError:\n    sys.exit(3)\n"
        )
        result = subprocess.run([sys.executable, "-c", code, str(tmp_path)], check=False, timeout=60)  # noqa: S603
        assert result.returncode == 3
    finally:
        lock.release()


def test_different_data_dirs_do_not_conflict(tmp_path):
    a, b = InstanceLock(tmp_path / "a"), InstanceLock(tmp_path / "b")
    a.acquire()
    b.acquire()
    a.release()
    b.release()


@pytest.mark.parametrize(("value", "ok"), [("", True), ("1", True), ("junk", True), ("2", False), ("4", False)])
def test_worker_count_must_be_one(monkeypatch, value, ok):
    monkeypatch.setenv("WEB_CONCURRENCY", value)
    if ok:
        check_worker_count()
    else:
        with pytest.raises(InstanceLockError, match="single worker"):
            check_worker_count()
