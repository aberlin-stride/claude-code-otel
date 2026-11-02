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


if __name__ == "__main__":
    unittest.main()
