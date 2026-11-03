"""Integration checks for the source or packaged desktop backend.

AGENT_HUB_TEST_BACKEND=/path/to/agent-hub-server python3 -m unittest discover -s desktop
"""

import json
import os
from pathlib import Path
import selectors
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def ready(process):
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        if not selector.select(timeout=15):
            raise AssertionError("Backend did not announce readiness")
    line = process.stdout.readline()
    if not line:
        raise AssertionError("Backend exited before readiness: " + process.stderr.read())
    return json.loads(line)


class DesktopBackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.environment = os.environ.copy()
        self.environment.update(AGENT_HUB_DATA_DIR=self.temp.name, PYTHONPATH=str(ROOT), PYTHONDONTWRITEBYTECODE="1")
        executable = os.environ.get("AGENT_HUB_TEST_BACKEND")
        self.command = [executable] if executable else [sys.executable, str(ROOT / "desktop/server_entry.py")]

    def start(self, demo=True, port=0):
        command = self.command + ["--parent-pid", str(os.getpid()), "--port", str(port)]
        if demo:
            command.append("--demo")
        process = subprocess.Popen(command, env=self.environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        def cleanup():
            if process.poll() is None:
                process.terminate()
            process.communicate(timeout=5)
        self.addCleanup(cleanup)
        info = ready(process)
        return process, f"http://127.0.0.1:{info['port']}", info

    def request(self, url, body=None, origin=None):
        headers = {"Origin": origin} if origin else {}
        if body is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None, headers=headers)
        return urllib.request.urlopen(req, timeout=5)

    def test_serves_dashboard_and_rejects_foreign_origin(self):
        _, base, _ = self.start()
        with self.request(base + "/api/sessions?lang=zh") as response:
            result = json.load(response)
        self.assertTrue(result["demo"])
        self.assertGreater(len(result["sessions"]), 0)
        self.assertEqual(result["errors"], {})
        with self.request(base + "/") as response:
            self.assertIn(b"<html", response.read())
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request(base + "/api/seen", {"app": "codex", "id": "demo-1"}, "https://example.com")
        self.assertEqual(error.exception.code, 403)

    def test_seen_state_survives_restart_outside_bundle(self):
        process, base, _ = self.start(demo=False)
        sid = "11111111-1111-1111-1111-111111111111"
        with self.request(base + "/api/seen", {"app": "codex", "id": sid}, base) as response:
            self.assertTrue(json.load(response)["ok"])
        state_path = Path(self.temp.name) / "agent_hub_state.json"
        before = json.loads(state_path.read_text())
        self.assertIn("codex:" + sid, before["seen"])
        process.terminate(); process.wait(timeout=5)
        self.start(demo=False)
        self.assertEqual(json.loads(state_path.read_text()), before)

    def test_busy_port_falls_back_and_shutdown_releases_port(self):
        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0)); occupied.listen()
            port = occupied.getsockname()[1]
            process, _, info = self.start(port=port)
            self.assertNotEqual(info["port"], port)
        process.terminate(); process.wait(timeout=5)
        self.assertEqual(process.returncode, 0)
        with socket.socket() as probe:
            probe.settimeout(1)
            self.assertNotEqual(probe.connect_ex(("127.0.0.1", info["port"])), 0)

    def test_backend_stops_if_native_parent_disappears(self):
        # This intermediate process stands in for the native application.
        script = """
import json,os,subprocess,sys
child=subprocess.Popen(json.loads(sys.argv[1])+['--demo','--port','0','--parent-pid',str(os.getpid())],stdout=subprocess.PIPE,text=True)
print(child.stdout.readline(),end='',flush=True)
sys.stdin.read()
"""
        parent = subprocess.Popen([sys.executable, "-c", script, json.dumps(self.command)],
                                  env=self.environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        info = ready(parent)
        try:
            parent.kill(); parent.wait(timeout=5)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                with socket.socket() as probe:
                    probe.settimeout(0.2)
                    if probe.connect_ex(("127.0.0.1", info["port"])) != 0:
                        break
                time.sleep(0.1)
            else:
                self.fail("Backend still listens after its parent has died")
        finally:
            if parent.poll() is None:
                parent.kill()
            parent.communicate(timeout=5)
            try:
                os.kill(info["pid"], signal.SIGTERM)
            except ProcessLookupError:
                pass


if __name__ == "__main__":
    unittest.main()
