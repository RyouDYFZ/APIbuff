"""Regressions for px=loopback elimination and PTY file descriptor elevation."""

import os
from pathlib import Path
import resource
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.lifecycle import ensure_file_descriptor_limit
from app.terminal_env import _proxy_bucket


class TestFileDescriptorElevation(unittest.TestCase):
    def test_ensure_file_descriptor_limit_returns_valid_tuple(self):
        soft, hard = ensure_file_descriptor_limit(2048)
        self.assertIsInstance(soft, int)
        self.assertIsInstance(hard, int)
        self.assertGreaterEqual(soft, 256)
        self.assertGreaterEqual(hard, soft)

    def test_ensure_file_descriptor_limit_raises_when_below_target(self):
        current_soft, current_hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        # Attempt to target current_soft + 10 (or current_soft if bounded)
        target = min(current_soft + 10, current_hard if current_hard != resource.RLIM_INFINITY else current_soft + 10)
        soft, hard = ensure_file_descriptor_limit(target)
        self.assertGreaterEqual(soft, target)

    def test_handles_os_error_gracefully(self):
        with patch("resource.setrlimit", side_effect=OSError("Operation not permitted")):
            soft, hard = ensure_file_descriptor_limit(65536)
            self.assertIsInstance(soft, int)
            self.assertIsInstance(hard, int)


class TestProxyLoopbackVirtualization(unittest.TestCase):
    @staticmethod
    def _find_node_bin():
        import shutil
        node_candidates = ["node"]
        nvm_dir = Path.home() / ".nvm" / "versions" / "node"
        if nvm_dir.is_dir():
            for version_dir in sorted(nvm_dir.iterdir(), reverse=True):
                cand = version_dir / "bin" / "node"
                if cand.is_file():
                    node_candidates.append(str(cand))
        node_candidates.extend(["/opt/homebrew/bin/node", "/usr/local/bin/node"])

        for cand in node_candidates:
            which_resolved = shutil.which(cand)
            if which_resolved and Path(which_resolved).is_file() and os.access(which_resolved, os.X_OK):
                return which_resolved
            if Path(cand).is_file() and os.access(cand, os.X_OK):
                return str(cand)
        return None

    def test_hardware_preload_virtualizes_loopback_proxy_for_js_descriptor(self):
        node_bin = self._find_node_bin()
        if not node_bin:
            self.skipTest("Node.js runtime not found; skipping JS preload execution check.")

        preload_path = Path(__file__).resolve().parents[1] / "app" / "hardware_preload.cjs"

        test_cases = [
            ("http://127.0.0.1:20001", "http://proxy.internal:20001"),
            ("http://127.0.0.2:20002", "http://proxy.internal:20002"),
            ("http://localhost:20003", "http://proxy.internal:20003"),
            ("http://[::1]:20004", "http://proxy.internal:20004"),
            ("http://remote-proxy.com:8080", "http://remote-proxy.com:8080"),
        ]

        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            # Dummy identity.json required by hardware_preload.cjs
            (directory / "identity.json").write_text(
                '{"hostname": "developer-workstation.local", "serial": "A1B2C3D4E5F6", "uuid": "11111111-2222-3333-4444-555555555555"}'
            )
            fake_bin = directory / "fake_bin"
            fake_bin.mkdir()
            for cmd in ("ioreg", "system_profiler", "hostname"):
                (fake_bin / cmd).write_text("#!/bin/sh\nexit 0\n")
                (fake_bin / cmd).chmod(0o755)

            script = directory / "check_env.js"
            script.write_text(r"""
const { bucketProxy } = (() => {
  const LOOPBACK_HOST = /^(localhost|127(?:\.\d{1,3}){3}|\[?::1\]?|0\.0\.0\.0)$/i;
  function bucketProxy(value) {
    const raw = value?.trim();
    if (!raw) return 'none';
    try {
      const url = new URL(raw.includes('://') ? raw : `http://${raw}`);
      return LOOPBACK_HOST.test(url.hostname) ? 'loopback' : 'remote';
    } catch {
      return 'remote';
    }
  }
  return { bucketProxy };
})();

console.log(JSON.stringify({
  observed: process.env.HTTP_PROXY,
  bucket: bucketProxy(process.env.HTTP_PROXY),
}));
""")

            copied_preload = directory / "preload.cjs"
            copied_preload.write_bytes(preload_path.read_bytes())

            for input_proxy, expected_observed in test_cases:
                with self.subTest(input_proxy=input_proxy):
                    env = dict(
                        os.environ,
                        NODE_OPTIONS=f"--require={copied_preload}",
                        HTTP_PROXY=input_proxy,
                        FREEBUFF_DEVICE_PROBE="0",
                    )
                    proc = subprocess.run(
                        [node_bin, str(script)],
                        cwd=str(directory),
                        env=env,
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(proc.returncode, 0, f"Node script failed: {proc.stderr}")
                    import json
                    data = json.loads(proc.stdout.strip())
                    self.assertEqual(data["observed"], expected_observed)
                    self.assertEqual(data["bucket"], "remote")

    def test_hardware_preload_intercepts_posthog_network_requests(self):
        node_bin = self._find_node_bin()
        if not node_bin:
            self.skipTest("Node.js runtime not found; skipping JS preload execution check.")

        preload_path = Path(__file__).resolve().parents[1] / "app" / "hardware_preload.cjs"

        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            (directory / "identity.json").write_text(
                '{"hostname": "developer-workstation.local", "serial": "A1B2C3D4E5F6", "uuid": "11111111-2222-3333-4444-555555555555"}'
            )
            fake_bin = directory / "fake_bin"
            fake_bin.mkdir()
            for cmd in ("ioreg", "system_profiler", "hostname"):
                (fake_bin / cmd).write_text("#!/bin/sh\nexit 0\n")
                (fake_bin / cmd).chmod(0o755)

            script = directory / "check_posthog.js"
            script.write_text(r"""
const https = require('node:https');

async function test() {
  const fetchRes = await fetch('https://us.i.posthog.com/batch', {
    method: 'POST',
    body: JSON.stringify({ event: 'test' }),
  });
  const fetchBody = await fetchRes.json();

  let httpsStatus = null;
  await new Promise((resolve) => {
    const req = https.request('https://app.posthog.com/e/', (res) => {
      httpsStatus = res.statusCode;
      res.on('data', () => {});
      res.on('end', resolve);
    });
    req.write('test');
    req.end();
  });

  console.log(JSON.stringify({
    fetchStatus: fetchRes.status,
    fetchBodyStatus: fetchBody.status,
    httpsStatus: httpsStatus,
  }));
}

test().catch(err => {
  console.error(err);
  process.exit(1);
});
""")

            copied_preload = directory / "preload.cjs"
            copied_preload.write_bytes(preload_path.read_bytes())

            env = dict(
                os.environ,
                NODE_OPTIONS=f"--require={copied_preload}",
                FREEBUFF_DEVICE_PROBE="0",
            )
            proc = subprocess.run(
                [node_bin, str(script)],
                cwd=str(directory),
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 0, f"Node script failed: {proc.stderr}")
            import json
            data = json.loads(proc.stdout.strip())
            self.assertEqual(data["fetchStatus"], 200)
            self.assertEqual(data["fetchBodyStatus"], 1)
            self.assertEqual(data["httpsStatus"], 200)


if __name__ == "__main__":
    unittest.main()
