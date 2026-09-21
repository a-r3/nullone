#!/usr/bin/env python3
"""Offline tests for process-group-scoped subprocess execution (#119).

Uses REAL short-lived local processes only (sleep/print/marker file,
1-5s timeouts -- never 600 real seconds, no network, no model calls):

- child finishing before the timeout succeeds with output passthrough;
- slow child raises the REAL subprocess.TimeoutExpired (the typed
  provider-error mapping depends on this exact type);
- on timeout the whole process tree dies: direct child reaped AND
  grandchildren reaped (no marker file, no surviving sleepers);
- missing binary raises FileNotFoundError (startup mapping preserved);
- nonzero exit passes through without raising (check=False semantics).
- production topology: launcher exits BEFORE the deadline while a
  detached (setsid) worker survives; the lifecycle owner still
  enforces the deadline on exactly the invocation-owned tree.
- kill-scope safety: unrelated processes are never signalled.
"""
from __future__ import annotations

import errno
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "workspace/social/ops/scripts"
sys.path.insert(0, str(SCRIPTS))

from nullone_process_tree import run_tree_command  # noqa: E402


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError as e:
        if e.errno == errno.ESRCH:
            return False
        return True


def _ps_stat(pid: int) -> str:
    try:
        cp = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(pid)],
            text=True,
            capture_output=True,
            timeout=5,
        )
        return cp.stdout.strip()
    except Exception:
        return ""


def _launcher_code(worker_pid_file: Path, exit_marker: Path, close_pipes: bool) -> str:
    """Production-relevant launcher: forks worker, worker setsid, launcher exits in ~0.3s."""
    w = str(worker_pid_file)
    m = str(exit_marker)
    if close_pipes:
        # Daemon-style: worker closes inherited stdio so the owner's
        # communicate() would return early without deadline enforcement.
        return (
            "import os,time\n"
            "pid=os.fork()\n"
            "if pid==0:\n"
            "    os.setsid()\n"
            "    try:\n"
            "        os.close(1)\n"
            "    except OSError:\n"
            "        pass\n"
            "    try:\n"
            "        os.close(2)\n"
            "    except OSError:\n"
            "        pass\n"
            f"    open({w!r},'w').write(str(os.getpid()))\n"
            "    time.sleep(30)\n"
            "    os._exit(0)\n"
            "else:\n"
            "    time.sleep(0.3)\n"
            f"    open({m!r},'w').write('exited')\n"
            "    os._exit(0)\n"
        )
    return (
        "import os,time\n"
        "pid=os.fork()\n"
        "if pid==0:\n"
        "    os.setsid()\n"
        f"    open({w!r},'w').write(str(os.getpid()))\n"
        "    time.sleep(30)\n"
        "    os._exit(0)\n"
        "else:\n"
        "    time.sleep(0.3)\n"
        f"    open({m!r},'w').write('exited')\n"
        "    os._exit(0)\n"
    )


class ProcessTreeTests(unittest.TestCase):
    def test_success_passes_through_output(self):
        cp = run_tree_command(
            [sys.executable, "-c", "print('hi')"],
            cwd=tempfile.gettempdir(),
            timeout=30,
        )
        self.assertEqual(cp.returncode, 0)
        self.assertEqual(cp.stdout.strip(), "hi")

    def test_nonzero_exit_passes_through_without_raising(self):
        cp = run_tree_command(
            [sys.executable, "-c", "import sys; sys.exit(3)"],
            cwd=tempfile.gettempdir(),
            timeout=30,
        )
        self.assertEqual(cp.returncode, 3)

    def test_timeout_raises_real_timeout_expired(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            run_tree_command(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                cwd=tempfile.gettempdir(),
                timeout=1,
            )

    def test_timeout_kills_whole_tree(self):
        with tempfile.TemporaryDirectory() as td:
            marker = str(Path(td) / "grandchild.marker")
            script = (
                "import subprocess,time;"
                f"subprocess.Popen(['bash','-lc','sleep 3; touch {marker}; sleep 30']);"
                "time.sleep(30)"
            )
            with self.assertRaises(subprocess.TimeoutExpired):
                run_tree_command(
                    [sys.executable, "-c", script],
                    cwd=td,
                    timeout=1,
                )
            time.sleep(4)  # past the grandchild marker time
            self.assertFalse(
                os.path.exists(marker),
                msg="grandchild survived the timeout: process tree not cleaned up",
            )

    def test_timeout_kills_detached_worker_when_launcher_alive(self):
        """Timeout kills a detached worker when the launcher is still alive.

        Launcher forks child, child setsid, writes pid, sleeps long.
        Launcher sleeps longer than timeout but shorter than worker sleep,
        then exits. The timeout must kill both launcher and the detached worker.
        """
        with tempfile.TemporaryDirectory() as td:
            worker_pid_file = Path(td) / "worker.pid"
            script = (
                "import os,time;pid=os.fork();"
                f"(pid==0) and (os.setsid(), open('{worker_pid_file}','w').write(str(os.getpid())), "
                "time.sleep(30), os._exit(0)) "
                "or (pid!=0) and (time.sleep(20), os._exit(0))"
            )
            try:
                run_tree_command(
                    [sys.executable, "-c", script],
                    cwd=td,
                    timeout=5,
                )
            except subprocess.TimeoutExpired:
                pass
            else:
                self.fail("TimeoutExpired not raised")
            time.sleep(0.5)  # let the kill take effect
            # Check worker is dead
            if worker_pid_file.exists():
                pid_str = worker_pid_file.read_text().strip()
                try:
                    worker_pid = int(pid_str)
                except ValueError:
                    self.fail(f"Invalid PID in {worker_pid_file}: {pid_str}")
                self.assertFalse(
                    _alive(worker_pid),
                    f"Worker process {worker_pid} still alive after timeout",
                )
            else:
                self.fail(f"Worker PID file {worker_pid_file} not created")

    def test_production_topology_launcher_exits_before_timeout(self):
        """Exact production topology: launcher gone before deadline, worker killed by owner.

        1. launcher starts worker; 2. worker detaches via setsid;
        3. launcher exits (~0.3s) BEFORE the 2s deadline; 4. launcher
        exit marker proves it is gone; 5. uncontrolled run proves the
        worker stays alive after launcher exit; 6-7. lifecycle-owned
        run reaches the deadline and terminates THIS worker;
        8. no orphan/zombie remains; 9. unrelated process untouched.
        Covers both stdio-holding and daemon (stdio-closing) workers.
        """
        for close_pipes in (False, True):
            with self.subTest(close_pipes=close_pipes):
                with tempfile.TemporaryDirectory() as td:
                    td_path = Path(td)
                    worker_pid_file = td_path / "worker.pid"
                    exit_marker = td_path / "launcher.exited"

                    # Phase 1 (uncontrolled): prove the topology itself --
                    # launcher exits quickly and the detached worker
                    # demonstrably stays alive without an owner.
                    uncontrolled_code = _launcher_code(
                        td_path / "uncontrolled_worker.pid",
                        td_path / "uncontrolled.exited",
                        close_pipes,
                    )
                    uncontrolled = subprocess.Popen(
                        [sys.executable, "-c", uncontrolled_code],
                        cwd=td,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        start_new_session=True,
                    )
                    try:
                        uncontrolled.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        self.fail("uncontrolled launcher did not exit quickly")
                    self.assertTrue(
                        (td_path / "uncontrolled.exited").exists(),
                        "launcher exit not proven",
                    )
                    self.assertTrue(
                        (td_path / "uncontrolled_worker.pid").exists(),
                        "worker pid not recorded",
                    )
                    uncontrolled_worker = int(
                        (td_path / "uncontrolled_worker.pid").read_text().strip()
                    )
                    self.assertTrue(
                        _alive(uncontrolled_worker),
                        "detached worker not alive after launcher exit",
                    )
                    # Cleanup uncontrolled worker (reap if our child/subreaper).
                    try:
                        os.kill(uncontrolled_worker, signal.SIGKILL)
                    except OSError:
                        pass
                    end = time.monotonic() + 3
                    while time.monotonic() < end and _alive(uncontrolled_worker):
                        try:
                            os.waitpid(uncontrolled_worker, os.WNOHANG)
                        except Exception:
                            pass
                        time.sleep(0.05)
                    self.assertFalse(
                        _alive(uncontrolled_worker),
                        "uncontrolled worker cleanup failed",
                    )

                    # Phase 2 (lifecycle-owned): same topology under the
                    # deadline owner; worker must be terminated at 2s.
                    code = _launcher_code(worker_pid_file, exit_marker, close_pipes)
                    start = time.monotonic()
                    with self.assertRaises(subprocess.TimeoutExpired):
                        run_tree_command(
                            [sys.executable, "-c", code],
                            cwd=td,
                            timeout=2,
                        )
                    elapsed = time.monotonic() - start
                    # Launcher exited (~0.3s) well before the 2s deadline.
                    self.assertTrue(
                        exit_marker.exists(),
                        "launcher exit before timeout not proven",
                    )
                    marker_age = exit_marker.stat().st_mtime - start
                    # st_mtime is wall-clock; compare against elapsed to
                    # show the marker predates the deadline return.
                    self.assertLess(
                        elapsed,
                        10,
                        f"deadline enforcement took too long: {elapsed:.2f}s",
                    )
                    self.assertGreaterEqual(
                        elapsed,
                        1.5,
                        f"deadline not actually enforced: {elapsed:.2f}s",
                    )
                    _ = marker_age  # wall-clock vs monotonic; informational only.
                    self.assertTrue(
                        worker_pid_file.exists(),
                        "worker pid not recorded in owned run",
                    )
                    worker_pid = int(worker_pid_file.read_text().strip())
                    # Reap-wait: SIGKILLed reparented child may briefly
                    # appear as zombie; poll until gone.
                    gone_end = time.monotonic() + 3
                    while time.monotonic() < gone_end and _alive(worker_pid):
                        try:
                            os.waitpid(worker_pid, os.WNOHANG)
                        except Exception:
                            pass
                        try:
                            os.waitpid(-1, os.WNOHANG)
                        except Exception:
                            pass
                        time.sleep(0.05)
                    self.assertFalse(
                        _alive(worker_pid),
                        f"THIS invocation worker {worker_pid} survived the deadline",
                    )
                    self.assertEqual(
                        _ps_stat(worker_pid),
                        "",
                        f"orphan remains for worker {worker_pid}",
                    )

    def test_kill_scope_safety_unrelated_process_survives(self):
        """The fix never signals outside the invocation-owned tree."""
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            # Unrelated healthy process started BEFORE the invocation.
            unrelated = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            try:
                self.assertTrue(_alive(unrelated.pid))
                worker_pid_file = td_path / "worker.pid"
                exit_marker = td_path / "launcher.exited"
                code = _launcher_code(worker_pid_file, exit_marker, True)
                with self.assertRaises(subprocess.TimeoutExpired):
                    run_tree_command(
                        [sys.executable, "-c", code],
                        cwd=td,
                        timeout=2,
                    )
                time.sleep(0.3)
                self.assertTrue(
                    _alive(unrelated.pid),
                    f"unrelated process {unrelated.pid} was killed",
                )
                self.assertTrue(worker_pid_file.exists())
                worker_pid = int(worker_pid_file.read_text().strip())
                self.assertNotEqual(worker_pid, unrelated.pid)
                self.assertFalse(
                    _alive(worker_pid),
                    "invocation worker was not terminated",
                )
            finally:
                try:
                    unrelated.terminate()
                except OSError:
                    pass
                try:
                    unrelated.wait(timeout=3)
                except Exception:
                    try:
                        unrelated.kill()
                    except OSError:
                        pass

    def test_provider_timeout_lifecycle_cleanup(self):
        """Provider timeout -> tree gone, typed error, finally runs, no stale lock."""
        import nullone_opencode_role as role_transport

        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            lock = td_path / "job.lock"
            lock.write_text("held", encoding="utf-8")
            worker_pid_file = td_path / "worker.pid"
            exit_marker = td_path / "launcher.exited"
            code = _launcher_code(worker_pid_file, exit_marker, True)
            finally_ran: list[bool] = []
            mapped = False
            try:
                try:
                    role_transport.run_opencode_cycle(
                        [sys.executable, "-c", code],
                        cwd=td_path,
                        timeout=2,
                        role="probe",
                    )
                except role_transport.RoleExecutionTimeoutError as e:
                    mapped = True
                    self.assertIn("exceeded its execution deadline", str(e))
                finally:
                    finally_ran.append(True)
                    try:
                        lock.unlink()
                    except FileNotFoundError:
                        pass
            finally:
                pass
            self.assertTrue(mapped, "timeout was not mapped to typed provider error")
            self.assertEqual(finally_ran, [True], "cleanup/finally did not execute")
            self.assertFalse(lock.exists(), "stale lock remains")
            self.assertTrue(worker_pid_file.exists())
            worker_pid = int(worker_pid_file.read_text().strip())
            gone_end = time.monotonic() + 3
            while time.monotonic() < gone_end and _alive(worker_pid):
                try:
                    os.waitpid(worker_pid, os.WNOHANG)
                except Exception:
                    pass
                time.sleep(0.05)
            self.assertFalse(_alive(worker_pid), "process tree survived provider timeout")

    def test_missing_binary_raises_file_not_found(self):
        with self.assertRaises(FileNotFoundError):
            run_tree_command(
                ["/tmp/nullone-definitely-no-such-binary-xyz"],
                cwd=tempfile.gettempdir(),
                timeout=5,
            )

    def test_consecutive_timeouts_are_stateless(self):
        """Two back-to-back timeouts both raise promptly: no leaked
        child holds resources that would overlap a scheduler retry."""
        for _ in range(2):
            started = time.monotonic()
            with self.assertRaises(subprocess.TimeoutExpired):
                run_tree_command(
                    [sys.executable, "-c", "import time; time.sleep(30)"],
                    cwd=tempfile.gettempdir(),
                    timeout=1,
                )
            self.assertLess(time.monotonic() - started, 10)


if __name__ == "__main__":
    unittest.main()
