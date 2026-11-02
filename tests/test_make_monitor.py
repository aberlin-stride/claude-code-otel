import os
import signal
import subprocess
import tempfile
import time
import unittest

REPO = os.path.join(os.path.dirname(__file__), "..")


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class MakeMonitorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.pidfile = os.path.join(self.tmp.name, "monitor.pid")
        self.logfile = os.path.join(self.tmp.name, "monitor.log")
        # Port 1 refuses connections: the monitor runs but never touches real Loki.
        self.env = dict(os.environ, LOKI_URL="http://127.0.0.1:1")
        self.addCleanup(self.cleanup)

    def cleanup(self):
        if os.path.exists(self.pidfile):
            with open(self.pidfile) as f:
                try:
                    os.kill(int(f.read()), signal.SIGKILL)
                except (ProcessLookupError, ValueError):
                    pass
        self.tmp.cleanup()

    def make(self, target):
        return subprocess.run(
            ["make", target, "MONITOR_PID=" + self.pidfile, "MONITOR_LOG=" + self.logfile],
            cwd=REPO, env=self.env, capture_output=True, text=True, timeout=30)

    def write_stale_pidfile(self):
        finished = subprocess.Popen(["true"])
        finished.wait()
        with open(self.pidfile, "w") as f:
            f.write(str(finished.pid))

    def read_pid(self):
        with open(self.pidfile) as f:
            return int(f.read())

    def test_monitor_target_starts_a_background_process(self):
        self.make("monitor")
        self.assertTrue(os.path.exists(self.pidfile))
        self.assertTrue(alive(self.read_pid()))

    def test_monitor_target_is_idempotent(self):
        self.make("monitor")
        first = self.read_pid()
        self.make("monitor")
        self.assertEqual(self.read_pid(), first)
        self.assertTrue(alive(first))

    def test_stop_monitor_stops_the_process_and_removes_the_pidfile(self):
        self.make("monitor")
        pid = self.read_pid()
        self.make("stop-monitor")
        deadline = time.time() + 2
        while alive(pid) and time.time() < deadline:
            time.sleep(0.05)
        self.assertFalse(alive(pid))
        self.assertFalse(os.path.exists(self.pidfile))

    def test_stop_monitor_without_a_pidfile_succeeds(self):
        self.assertEqual(self.make("stop-monitor").returncode, 0)

    # Guard added after the recipe existed: passed on first run (the recipe
    # ignores kill's exit status), so it was never RED.
    def test_stop_monitor_tolerates_a_stale_pidfile(self):
        self.write_stale_pidfile()
        self.assertEqual(self.make("stop-monitor").returncode, 0)
        self.assertFalse(os.path.exists(self.pidfile))

    # Guard added after the recipe existed (kill -0 already rejects dead pids), never RED.
    def test_monitor_replaces_a_stale_pidfile_with_a_live_process(self):
        self.write_stale_pidfile()
        stale = self.read_pid()
        self.make("monitor")
        self.assertNotEqual(self.read_pid(), stale)
        self.assertTrue(alive(self.read_pid()))


if __name__ == "__main__":
    unittest.main()
