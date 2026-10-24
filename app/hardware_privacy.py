"""Protect the known CLI hardware probes on macOS and Linux without changing the host.

This is a guard for the extracted Bun client's collection paths, not a general
VM or a security boundary against arbitrary code running under the same UID.
"""

import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile

from .hardware_shims import hardware_identity, shim_sources


SANDBOX = '/usr/bin/sandbox-exec'
# Prevent a missing PATH shim, nested shell, or future absolute command from
# silently reaching the real tools on macOS. JS redirects the known direct call forms.
HARDWARE_SANDBOX = '''(version 1)
(allow default)
(deny process-exec
  (literal "/usr/sbin/ioreg")
  (literal "/usr/sbin/system_profiler")
  (literal "/bin/hostname")
  (literal "/usr/bin/hostname"))
'''
PROBE_MARKER = 'FREEBUFF_DEVICE_PROBE_OK_v1'


class HardwarePrivacyError(RuntimeError):
    pass


def _private_directory(path):
    path.mkdir(mode=0o700, exist_ok=True)
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or
            stat.S_IMODE(info.st_mode) != 0o700):
        raise HardwarePrivacyError('Hardware privacy cache must be a private, owned directory without symlinks.')


def ensure_linux_preload_so():
    """Locate or compile the Linux LD_PRELOAD .so library when running on Linux."""
    if not sys.platform.startswith('linux'):
        return None
    app_dir = Path(__file__).resolve().parent
    for candidate in (app_dir / 'libhardware_privacy.so', app_dir.parent / 'libhardware_privacy.so'):
        if candidate.is_file():
            return candidate
    source = app_dir / 'linux_hardware_privacy.c'
    if not source.is_file():
        return None
    root = Path('/tmp').resolve() / f'freebuff-hardware-{os.getuid()}'
    root.mkdir(mode=0o700, exist_ok=True)
    target = root / 'libhardware_privacy.so'
    if target.is_file():
        return target
    compiler = shutil.which('gcc') or shutil.which('clang')
    if not compiler:
        return None
    try:
        subprocess.run(
            [compiler, '-shared', '-fPIC', '-O2', '-o', str(target), str(source), '-ldl'],
            check=True,
            capture_output=True,
            timeout=10,
        )
        target.chmod(0o700)
        return target
    except Exception:
        return None


def prepare_profile(account_id):
    """Create reproducible assets at a path Bun 1.3 BUN_OPTIONS can parse."""
    identity = hardware_identity(account_id)
    assets = {
        'identity.json': json.dumps(identity, sort_keys=True).encode(),
        'preload.cjs': Path(__file__).with_name('hardware_preload.cjs').read_bytes(),
        'synthetic/machine-id': (identity['machine_id'] + '\n').encode(),
        'synthetic/product_uuid': (identity['uuid'].lower() + '\n').encode(),
        'synthetic/product_serial': (identity['serial'] + '\n').encode(),
        'synthetic/product_name': b'Virtual Machine\n',
        'synthetic/product_version': b'1.0\n',
        'synthetic/sys_vendor': b'Cloud Provider\n',
    }
    for name, content in shim_sources(identity).items():
        assets['fake_bin/' + name] = content.encode() if isinstance(content, str) else content
    digest = hashlib.sha256()
    for name, content in sorted(assets.items()):
        digest.update(name.encode() + b'\0' + content + b'\0')
    # Do not use TMPDIR or account home here: BUN_OPTIONS does not reliably
    # parse quoted preload paths containing spaces in the installed Bun 1.3.
    root = Path('/tmp').resolve() / f'freebuff-hardware-{os.getuid()}'
    directory = root / digest.hexdigest()
    if not re.fullmatch(r'[A-Za-z0-9_./-]+', str(directory)):
        raise HardwarePrivacyError('Hardware privacy preload path is not supported by Bun.')
    for folder in (root, directory, directory / 'fake_bin', directory / 'synthetic'):
        _private_directory(folder)
    for name, content in assets.items():
        target = directory / name
        # Atomic replacement avoids following an existing file symlink and
        # prevents another worker from reading half of a generated asset.
        fd, temporary = tempfile.mkstemp(dir=target.parent)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(content)
                os.fchmod(stream.fileno(), 0o700 if name.startswith('fake_bin/') else 0o600)
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return directory, identity


def _shell_command(args, shell, login, assignments=None, prelude='', path_prefix=None):
    command = [shell]
    if login:
        command.append('-l')
    prefix = ' '.join(f'{key}={shlex.quote(value)}' for key, value in (assignments or {}).items())
    if path_prefix is not None:
        prefix += ' PATH=' + shlex.quote(str(path_prefix)) + ':"$PATH"'
    # A login profile must not switch this binary into Bun's generic runtime.
    cleanup = 'unset BUN_OPTIONS BUN_BE_BUN NODE_OPTIONS FREEBUFF_DEVICE_PROBE; '
    command.extend(['-c', prelude + cleanup + (prefix + ' ' if prefix else '') + shlex.join(args) + '; exit $?'])
    return command


def build_worker_command(args, env, account_id, shell, login, prelude=''):
    """Return the launch command, raising before a worker can start unguarded.

    Mutates only the supplied child environment. Every guarded launch probes
    the real executable under the same shell and preload, with networking off.
    """
    mode = env.get('FREEBUFF_HARDWARE_PRIVACY', 'auto').strip().lower()
    if mode not in ('auto', 'macos', 'linux', 'off'):
        raise HardwarePrivacyError('FREEBUFF_HARDWARE_PRIVACY must be auto, macos, linux, or off.')
    # Inherited runtime flags must not replace the CLI entrypoint or install a
    # competing preload in either guarded or explicitly unguarded mode.
    for key in ('BUN_OPTIONS', 'BUN_BE_BUN', 'NODE_OPTIONS', 'FREEBUFF_DEVICE_PROBE', 'LD_PRELOAD', 'FREEBUFF_SYNTHETIC_DIR', 'FREEBUFF_SYNTHETIC_HOSTNAME'):
        env.pop(key, None)
    if mode == 'off':
        return _shell_command(args, shell, login, prelude=prelude)

    if mode == 'auto':
        if sys.platform == 'darwin':
            platform_mode = 'macos'
        elif sys.platform.startswith('linux'):
            platform_mode = 'linux'
        else:
            return _shell_command(args, shell, login, prelude=prelude)
    elif mode == 'macos':
        if sys.platform != 'darwin':
            raise HardwarePrivacyError('The macos hardware privacy mode requires macOS.')
        platform_mode = 'macos'
    elif mode == 'linux':
        if not sys.platform.startswith('linux'):
            raise HardwarePrivacyError('The linux hardware privacy mode requires Linux.')
        platform_mode = 'linux'

    binary = shutil.which(args[0], path=env.get('PATH')) if not os.path.isabs(args[0]) else args[0]
    try:
        with open(binary or '', 'rb') as stream:
            magic = stream.read(4)
    except OSError:
        magic = b''

    if platform_mode == 'macos':
        if magic not in (b'\xcf\xfa\xed\xfe', b'\xce\xfa\xed\xfe', b'\xca\xfe\xba\xbe', b'\xca\xfe\xba\xbf'):
            raise HardwarePrivacyError('Hardware privacy requires FREEBUFF_BIN to point to the native Bun CLI executable, not an npm or shell launcher.')
        if not os.path.isfile(SANDBOX):
            raise HardwarePrivacyError('macOS sandbox-exec is required for hardware privacy; worker was not started.')
    else:  # linux
        if magic != b'\x7fELF':
            raise HardwarePrivacyError('Hardware privacy requires FREEBUFF_BIN to point to the native Bun CLI executable, not an npm or shell launcher.')

    directory, identity = prepare_profile(account_id)
    assignments = {
        'BUN_OPTIONS': '--preload=' + str(directory / 'preload.cjs'),
        'HOSTNAME': identity['hostname'],
        'HOST': identity['hostname'],
    }
    if platform_mode == 'linux':
        so_path = ensure_linux_preload_so()
        if so_path and so_path.is_file():
            assignments['LD_PRELOAD'] = str(so_path)
            assignments['FREEBUFF_SYNTHETIC_DIR'] = str(directory)
            assignments['FREEBUFF_SYNTHETIC_HOSTNAME'] = identity['hostname']

    env.update(assignments)
    env['PATH'] = str(directory / 'fake_bin') + ':' + env.get('PATH', os.defpath)
    # Probe data cannot reach the account's credentials or the network even if
    # an unsupported executable ignores the preload and starts its application.
    with tempfile.TemporaryDirectory(prefix='freebuff-device-probe-') as probe_home:
        probe_env = dict(env, HOME=probe_home, FREEBUFF_CONFIG_DIR=probe_home + '/config')
        for key in ('CODEBUFF_API_KEY', 'FREEBUFF_AUTH_TOKEN', 'FREEBUFF_API_KEY'):
            probe_env.pop(key, None)
        probe_assignments = dict(assignments, FREEBUFF_DEVICE_PROBE='1')
        if platform_mode == 'macos':
            command = [SANDBOX, '-p', HARDWARE_SANDBOX + '(deny network*)\n',
                       *_shell_command([binary], shell, login, probe_assignments, prelude, directory / 'fake_bin')]
        else:
            command = _shell_command([binary], shell, login, probe_assignments, prelude, directory / 'fake_bin')
        try:
            with subprocess.Popen(command, env=probe_env, cwd=probe_home,
                                  stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True, start_new_session=True) as process:
                try:
                    output, _errors = process.communicate(timeout=12)
                except BaseException:
                    # Kill the probe shell and its descendants before reaping
                    # its PID. An unsupported app must not become an orphan.
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.communicate()
                    raise
        except (OSError, subprocess.SubprocessError):
            raise HardwarePrivacyError('Hardware privacy startup probe could not complete; worker was not started.') from None
        if process.returncode != 0 or output.strip() != PROBE_MARKER:
            # Never echo unknown CLI output: it may contain host identifiers.
            raise HardwarePrivacyError('Hardware privacy startup probe failed; worker was not started.')

    if platform_mode == 'macos':
        return [SANDBOX, '-p', HARDWARE_SANDBOX,
                *_shell_command([binary, *args[1:]], shell, login, assignments, prelude, directory / 'fake_bin')]
    else:
        return _shell_command([binary, *args[1:]], shell, login, assignments, prelude, directory / 'fake_bin')
