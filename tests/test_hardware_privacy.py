"""Offline probes only: never log in, submit a prompt, or print host IDs."""

import asyncio
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock
import uuid

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from app.hardware_privacy import (
    HARDWARE_SANDBOX, PROBE_MARKER, HardwarePrivacyError, build_worker_command, prepare_profile,
)
from app.hardware_shims import hardware_identity


class HardwareProfileTests(unittest.TestCase):
    def test_identity_is_stable_distinct_and_well_formed(self):
        first = hardware_identity('account-a')
        self.assertEqual(first, hardware_identity('account-a'))
        second = hardware_identity('account-b')
        self.assertNotEqual(first['uuid'], second['uuid'])
        self.assertNotEqual(first['serial'], second['serial'])
        self.assertNotEqual(first['machine_id'], second['machine_id'])
        # A random (v4) UUID shape, an Apple-alphabet serial without I/O, a
        # real model identifier, and a per-account .local hostname: values
        # shaped like genuine hardware rather than one shared synthetic form.
        self.assertEqual(uuid.UUID(first['uuid']).version, 4)
        self.assertRegex(first['serial'], r'^[A-HJ-NP-Z0-9]{12}$')
        self.assertNotIn('I', first['serial'])
        self.assertNotIn('O', first['serial'])
        self.assertRegex(first['machine_id'], r'^[a-f0-9]{32}$')
        self.assertRegex(first['hostname'], r'^[a-z0-9][a-z0-9-]*\.local$')
        self.assertNotEqual(first['hostname'], second['hostname'])
        self.assertIn(first['model'], ('Mac15,6', 'MacBookPro18,3', 'Mac14,2', 'Mac14,9', 'Macmini9,1', 'iMac21,1'))
        for value in ('', '../account', 'a/b', 'a b', None):
            with self.assertRaises(ValueError):
                hardware_identity(value)

    def test_shims_produce_only_synthetic_fields_and_deny_unknown_probes(self):
        directory, identity = prepare_profile('offline-shim-a')
        env = {'PATH': '/usr/bin:/bin'}
        # macOS probes
        for args in (['-rd1', '-c', 'IOPlatformExpertDevice'], ['-c', 'IOPlatformExpertDevice', '-d 2']):
            output = subprocess.check_output([str(directory / 'fake_bin/ioreg'), *args], env=env, text=True)
            parsed = output.split('IOPlatformUUID')[1].split('"')[2].lower()
            self.assertEqual(parsed, identity['uuid'].lower())
            self.assertIn('"IOPlatformSerialNumber" = "' + identity['serial'] + '"', output)
        output = subprocess.check_output([str(directory / 'fake_bin/system_profiler'), 'SPHardwareDataType', '-json'], env=env, text=True)
        fields = json.loads(output)['SPHardwareDataType'][0]
        self.assertEqual(fields['platform_UUID'], identity['uuid'])
        self.assertEqual(fields['serial_number'], identity['serial'])

        # Linux probes: dmidecode and cat shims
        dmi_output = subprocess.check_output([str(directory / 'fake_bin/dmidecode'), '-t', 'system'], env=env, text=True)
        self.assertIn(identity['uuid'], dmi_output)
        self.assertIn(identity['serial'], dmi_output)

        mid_output = subprocess.check_output([str(directory / 'fake_bin/cat'), '/etc/machine-id'], env=env, text=True).strip()
        self.assertEqual(mid_output, identity['machine_id'])

        uuid_output = subprocess.check_output([str(directory / 'fake_bin/cat'), '/sys/class/dmi/id/product_uuid'], env=env, text=True).strip()
        self.assertEqual(uuid_output.lower(), identity['uuid'].lower())

        serial_output = subprocess.check_output([str(directory / 'fake_bin/cat'), '/sys/class/dmi/id/product_serial'], env=env, text=True).strip()
        self.assertEqual(serial_output, identity['serial'])

        # Passthrough cat for normal files
        with tempfile.NamedTemporaryFile(mode='w', delete=False) as f:
            f.write('regular file content 42')
            temp_file = f.name
        try:
            cat_normal = subprocess.check_output([str(directory / 'fake_bin/cat'), temp_file], env=env, text=True).strip()
            self.assertEqual(cat_normal, 'regular file content 42')
        finally:
            os.unlink(temp_file)

        # Unknown tool probes are rejected
        for tool, args in [('ioreg', ['-l']), ('system_profiler', ['SPNetworkDataType']), ('hostname', ['new-name']), ('dmidecode', ['-s', 'bad'])]:
            result = subprocess.run([str(directory / 'fake_bin' / tool), *args], env=env, capture_output=True)
            self.assertEqual(result.returncode, 64)
            self.assertEqual(result.stdout, b'')

        self.assertEqual((directory / 'preload.cjs').stat().st_mode & 0o777, 0o600)
        self.assertEqual((directory / 'fake_bin/ioreg').stat().st_mode & 0o777, 0o700)
        self.assertEqual((directory / 'fake_bin/cat').stat().st_mode & 0o777, 0o700)
        self.assertEqual((directory / 'fake_bin/dmidecode').stat().st_mode & 0o777, 0o700)
        self.assertEqual((directory / 'synthetic/machine-id').stat().st_mode & 0o777, 0o600)
        self.assertEqual((directory / 'synthetic/machine-id').read_text().strip(), identity['machine_id'])
        self.assertEqual((directory / 'synthetic/product_uuid').read_text().strip(), identity['uuid'].lower())

    def test_platform_policy_and_inherited_runtime_flags(self):
        env = dict(os.environ, FREEBUFF_HARDWARE_PRIVACY='off', BUN_OPTIONS='bad', BUN_BE_BUN='1', NODE_OPTIONS='bad', FREEBUFF_DEVICE_PROBE='1')
        command = build_worker_command(['/fake/cli'], env, 'test', '/bin/sh', False)
        self.assertEqual(command[0], '/bin/sh')
        for key in ('BUN_OPTIONS', 'BUN_BE_BUN', 'NODE_OPTIONS', 'FREEBUFF_DEVICE_PROBE'):
            self.assertNotIn(key, env)

        with self.assertRaisesRegex(HardwarePrivacyError, 'auto, macos, linux, or off'):
            build_worker_command(['/fake/cli'], {'FREEBUFF_HARDWARE_PRIVACY': 'invalid'}, 'test', '/bin/sh', False)

        with patch('app.hardware_privacy.sys.platform', 'linux'):
            with self.assertRaisesRegex(HardwarePrivacyError, 'requires macOS'):
                build_worker_command(['/fake/cli'], {'FREEBUFF_HARDWARE_PRIVACY': 'macos'}, 'test', '/bin/sh', False)

        with patch('app.hardware_privacy.sys.platform', 'darwin'):
            with self.assertRaisesRegex(HardwarePrivacyError, 'requires Linux'):
                build_worker_command(['/fake/cli'], {'FREEBUFF_HARDWARE_PRIVACY': 'linux'}, 'test', '/bin/sh', False)

        with patch('app.hardware_privacy.sys.platform', 'darwin'):
            with self.assertRaisesRegex(HardwarePrivacyError, 'native Bun'):
                build_worker_command([__file__], {'FREEBUFF_HARDWARE_PRIVACY': 'macos'}, 'test', '/bin/sh', False)


class LinuxHardwareRuntimeTests(unittest.TestCase):
    def test_linux_binary_magic_enforcement(self):
        with tempfile.TemporaryDirectory() as temporary:
            elf_bin = Path(temporary) / 'fake-elf'
            elf_bin.write_bytes(b'\x7fELF\x02\x01\x01\x00' + b'\x00' * 16)
            elf_bin.chmod(0o700)

            macho_bin = Path(temporary) / 'fake-macho'
            macho_bin.write_bytes(b'\xcf\xfa\xed\xfe' + b'\x00' * 16)
            macho_bin.chmod(0o700)

            script_bin = Path(temporary) / 'fake-script'
            script_bin.write_bytes(b'#!/bin/sh\necho hi\n')
            script_bin.chmod(0o700)

            # On Linux, Mach-O or script is rejected
            with patch('app.hardware_privacy.sys.platform', 'linux'):
                with self.assertRaisesRegex(HardwarePrivacyError, 'native Bun CLI executable'):
                    build_worker_command([str(macho_bin)], {'FREEBUFF_HARDWARE_PRIVACY': 'linux'}, 'test', '/bin/sh', False)
                with self.assertRaisesRegex(HardwarePrivacyError, 'native Bun CLI executable'):
                    build_worker_command([str(script_bin)], {'FREEBUFF_HARDWARE_PRIVACY': 'linux'}, 'test', '/bin/sh', False)

    def test_linux_build_worker_command_skips_macos_sandbox(self):
        with tempfile.TemporaryDirectory() as temporary:
            elf_bin = Path(temporary) / 'fake-elf'
            elf_bin.write_bytes(b'\x7fELF\x02\x01\x01\x00' + b'\x00' * 16)
            elf_bin.chmod(0o700)

            # Mock successful probe
            mock_proc = MagicMock()
            mock_proc.communicate.return_value = (PROBE_MARKER, '')
            mock_proc.returncode = 0
            mock_proc.__enter__.return_value = mock_proc

            with patch('app.hardware_privacy.sys.platform', 'linux'), \
                 patch('app.hardware_privacy.subprocess.Popen', return_value=mock_proc):
                env = {'FREEBUFF_HARDWARE_PRIVACY': 'auto'}
                command = build_worker_command([str(elf_bin), '--arg1'], env, 'account-linux-1', '/bin/sh', False)
                # Ensure no sandbox-exec
                self.assertEqual(command[0], '/bin/sh')
                self.assertNotIn('/usr/bin/sandbox-exec', command)
                # Ensure assignments applied
                self.assertIn('BUN_OPTIONS', env)
                self.assertIn('--preload=', env['BUN_OPTIONS'])
                self.assertEqual(env['HOSTNAME'], hardware_identity('account-linux-1')['hostname'])
                self.assertIn('fake_bin', env['PATH'])

    def test_linux_preload_virtualization_in_node(self):
        node = shutil.which('node') or os.path.expanduser('~/.nvm/versions/node/v26.5.0/bin/node')
        if not os.path.isfile(node) or not os.access(node, os.X_OK):
            self.skipTest('Node binary not available for offline preload execution')

        profile, identity = prepare_profile('offline-linux-node')
        script = f"""
Object.defineProperty(process, 'platform', {{ value: 'linux', configurable: true }});
require('{profile / 'preload.cjs'}');
"""
        env = {
            'PATH': str(profile / 'fake_bin') + ':' + os.environ.get('PATH', '/usr/bin:/bin'),
            'FREEBUFF_DEVICE_PROBE': '1',
            'HOME': str(profile),
        }
        result = subprocess.run([node, '-e', script], cwd=str(profile), env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, f"Node Linux probe failed with stderr: {result.stderr}")
        self.assertEqual(result.stdout.strip(), PROBE_MARKER)


@unittest.skipUnless(sys.platform == 'darwin', 'macOS guard')
class MacHardwareRuntimeTests(unittest.TestCase):
    def test_ignored_preload_blocks_startup(self):
        with self.assertRaisesRegex(HardwarePrivacyError, 'startup probe failed'):
            build_worker_command(['/bin/echo'], {'PATH': '/usr/bin:/bin', 'FREEBUFF_HARDWARE_PRIVACY': 'macos'}, 'offline-unsupported', '/bin/sh', False)

    def test_failed_guard_does_not_allocate_a_worker_terminal(self):
        async def exercise_failure():
            from app.worker import FreebuffWorker
            with tempfile.TemporaryDirectory() as temporary:
                worker = FreebuffWorker(workdir=temporary, home_dir=temporary + '/home')
                with patch('app.worker.verify_private_cli'), patch.object(worker, '_ensure_git_repo'), patch('app.worker.build_worker_command', side_effect=HardwarePrivacyError('blocked')), patch('app.worker.pty.openpty') as terminal:
                    with self.assertRaises(HardwarePrivacyError):
                        await worker.start()
                    terminal.assert_not_called()
                    self.assertIsNone(worker.master_fd)
                    self.assertIsNone(worker._supervisor)
        asyncio.run(exercise_failure())

    def test_installed_cli_preload_exits_before_business_entrypoint(self):
        binary = os.environ.get('FREEBUFF_TEST_NATIVE_BIN', str(Path.home() / '.config/manicode/freebuff'))
        if not Path(binary).is_file():
            self.skipTest('Native CLI is not installed; no downloads in offline tests')
        for account in ('offline-runtime-a', 'offline-runtime-b', 'offline-runtime-a'):
            env = dict(os.environ, FREEBUFF_HARDWARE_PRIVACY='macos')
            command = build_worker_command([binary], env, account, '/bin/zsh', True)
            self.assertEqual(command[0], '/usr/bin/sandbox-exec')
            self.assertNotIn('FREEBUFF_DEVICE_PROBE=1', command[-1])
            self.assertNotIn('BUN_BE_BUN', env)

    def test_compiled_fixture_static_imports_async_apis_and_worker_launch(self):
        bun = shutil.which('bun')
        if not bun:
            self.skipTest('Bun is not installed; no downloads in offline tests')
        with tempfile.TemporaryDirectory(prefix="privacy fixture's ") as temporary:
            directory = Path(temporary)
            source = directory / 'fixture.ts'
            fixture_source = '''
import { hostname } from 'node:os';
import { exec, execFile, execFileSync, spawnSync } from 'node:child_process';
import { promisify } from 'node:util';
import { writeFileSync } from 'node:fs';
import assert from 'node:assert/strict';
const expectedHostname = __EXPECTED_HOSTNAME__;
const payload = JSON.parse(execFileSync('/usr/sbin/system_profiler', ['SPHardwareDataType', '-json'], {encoding:'utf8'})).SPHardwareDataType[0];
assert.equal(hostname(), expectedHostname);
const result = await promisify(exec)('/usr/sbin/ioreg -rd1 -c IOPlatformExpertDevice');
assert.equal(typeof result.stderr, 'string');
assert(result.stdout.includes(payload.platform_UUID));
const pending = promisify(execFile)('/bin/hostname', []);
assert(pending.child);
assert.equal((await pending).stdout.trim(), hostname());
assert.equal(spawnSync('/bin/hostname', [], {encoding:'utf8'}).stdout.trim(), hostname());
// A nested shell bypasses our JS redirect and must be stopped by the sandbox.
assert.notEqual(spawnSync('/bin/sh', ['-c','/bin/hostname']).status, 0);
const cwd = process.argv.indexOf('--cwd');
writeFileSync(process.argv[cwd + 1] + '/fixture-result.json', JSON.stringify(payload));
console.log('Enter a coding task');
setInterval(() => {}, 1000);
'''
            source.write_text(fixture_source.replace(
                '__EXPECTED_HOSTNAME__',
                json.dumps(hardware_identity('offline-worker')['hostname'])))
            binary = directory / 'compiled fixture'
            result = subprocess.run([bun, 'build', '--compile', str(source), '--outfile', str(binary)], cwd=directory, capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)

            async def exercise_worker():
                from app.worker import FreebuffWorker
                worker = FreebuffWorker(workdir=str(directory), home_dir=str(directory / "account's home"), account_id='offline-worker')
                with patch.dict(os.environ, {'FREEBUFF_HARDWARE_PRIVACY': 'macos'}), patch('app.worker.FREEBUFF_BIN', str(binary)), patch('app.worker.SHELL_PATH', '/bin/zsh'), patch('app.worker.USE_LOGIN_SHELL', True), patch('app.worker.verify_private_cli'), patch.object(worker, '_ensure_git_repo'):
                    try:
                        await worker.start()
                        fields = json.loads((directory / 'fixture-result.json').read_text())
                        self.assertEqual(fields['platform_UUID'], hardware_identity('offline-worker')['uuid'])
                        self.assertEqual(fields['serial_number'], hardware_identity('offline-worker')['serial'])
                    finally:
                        await worker.stop()
                self.assertIsNone(worker.master_fd)
            asyncio.run(exercise_worker())

    def test_original_libraries_consume_the_synthetic_profile(self):
        binary = Path.home() / '.config/manicode/freebuff'
        modules = PROJECT / 'extracted_sources/sources/node_modules'
        if not binary.is_file() or not modules.is_dir():
            self.skipTest('Optional local extracted source fixture is unavailable')
        with tempfile.TemporaryDirectory(prefix='privacy-library-probe-') as temporary:
            # No fingerprint module import: its logger/analytics would initialize
            # application state. Exercise its exact three library calls instead.
            script = Path(temporary) / 'probe.cjs'
            script.write_text('''
const assert = require('node:assert/strict');
const expected = JSON.parse(process.env.TEST_IDENTITY);
// uuid() also asks for MACs, outside this test's scope. Do not read them.
require('node:os').networkInterfaces = () => ({});
const { machineId } = require(process.env.TEST_MODULES + '/node-machine-id/dist/index.js');
const siSystem = require(process.env.TEST_MODULES + '/systeminformation/lib/system.js');
const siOS = require(process.env.TEST_MODULES + '/systeminformation/lib/osinfo.js');
const siCPU = require(process.env.TEST_MODULES + '/systeminformation/lib/cpu.js');
const { createHash } = require('node:crypto');
const machine = await machineId();
assert.equal(machine, createHash('sha256').update(expected.uuid.toLowerCase()).digest('hex'));
const [system, osInfo, cpu, ids] = await Promise.all([siSystem.system(), siOS.osInfo(), siCPU.cpu(), siOS.uuid()]);
assert.equal(system.serial, expected.serial);
assert.equal(system.uuid, expected.uuid.toLowerCase());
assert.equal(osInfo.hostname, expected.hostname);
assert.equal(osInfo.fqdn, expected.hostname);
assert.equal(ids.os, expected.uuid.toLowerCase());
assert.equal(ids.hardware, expected.serial);
assert.equal(typeof cpu.cores, 'number');
console.log('LIBRARY_PROBE_OK');
''')
            for account in ('offline-library-a', 'offline-library-b'):
                profile, identity = prepare_profile(account)
                env = {'PATH': str(profile / 'fake_bin') + ':/usr/bin:/bin', 'HOME': temporary,
                       'FREEBUFF_CONFIG_DIR': temporary + '/config', 'BUN_BE_BUN': '1',
                       'BUN_OPTIONS': '--preload=' + str(profile / 'preload.cjs'),
                       'TEST_IDENTITY': json.dumps(identity), 'TEST_MODULES': str(modules)}
                result = subprocess.run(['/usr/bin/sandbox-exec', '-p', HARDWARE_SANDBOX + '(deny network*)', str(binary), str(script)], env=env, cwd=temporary, capture_output=True, text=True, timeout=20)
                self.assertEqual(result.returncode, 0, 'Original library privacy probe failed (output withheld)')
                self.assertEqual(result.stdout.strip(), 'LIBRARY_PROBE_OK')


if __name__ == '__main__':
    unittest.main()
