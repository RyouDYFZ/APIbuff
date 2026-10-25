"""Synthetic outputs for the macOS and Linux probes used by the extracted CLI.

Every value is deterministic per account and shaped like a real one: a
version-4 UUID (Apple IOPlatformUUID values are random v4 form, never the
namespace-derived v5 form), a 12-character serial from Apple's serial alphabet
(which omits I and O), a genuine model identifier, and a per-account
``<owner>s-<machine>.local`` hostname in the macOS default style. Identical
synthetic shapes across every account would themselves be a fingerprint.
"""

import hashlib
import json
import random
import re
import uuid

# Apple serial numbers omit I and O to avoid confusion with 1 and 0.
SERIAL_ALPHABET = "ABCDEFGHJKLMNPQRSTVWXYZ0123456789"
MAC_MODELS = (
    ("MacBook Pro", "Mac15,6"),
    ("MacBook Pro", "MacBookPro18,3"),
    ("MacBook Air", "Mac14,2"),
    ("MacBook Air", "Mac14,9"),
    ("Mac mini", "Macmini9,1"),
    ("iMac", "iMac21,1"),
)
HOSTNAME_OWNERS = (
    "alex", "sam", "jordan", "taylor", "morgan", "casey",
    "riley", "jamie", "drew", "quinn", "avery", "blake",
)
HOSTNAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}\.local$")
SERIAL_PATTERN = re.compile(r"^[A-HJ-NP-Z0-9]{12}$")


def hardware_identity(account_id):
    if not isinstance(account_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', account_id):
        raise ValueError('A valid account ID is required for hardware privacy.')
    seed = hashlib.sha256(('freebuff:hardware:v2:' + account_id).encode()).digest()
    generator = random.Random(seed)
    uuid_v4 = uuid.UUID(int=generator.getrandbits(128), version=4)
    machine_name, model = MAC_MODELS[seed[0] % len(MAC_MODELS)]
    owner = HOSTNAME_OWNERS[seed[1] % len(HOSTNAME_OWNERS)]
    hostname = f"{owner}s-{machine_name.lower().replace(' ', '-')}.local"
    # Locally administered, unicast MAC address (02:xx:xx:xx:xx:xx)
    first_octet = (generator.getrandbits(8) & 0xFE) | 0x02
    other_octets = [generator.getrandbits(8) for _ in range(5)]
    mac_address = ':'.join(f'{b:02x}' for b in [first_octet] + other_octets).lower()
    return {
        'uuid': str(uuid_v4).upper(),
        'serial': ''.join(generator.choice(SERIAL_ALPHABET) for _ in range(12)),
        'hostname': hostname,
        'mac_address': mac_address,
        'machine_id': hashlib.sha256(('freebuff:machineid:v1:' + account_id).encode()).hexdigest()[:32].lower(),
        'machine_name': machine_name,
        'model': model,
    }


def shim_sources(identity):
    """No shim executes a real probe, even for an unrecognized argument list."""
    board_id = 'Mac-' + hashlib.sha256(identity['serial'].encode()).hexdigest()[:8].upper()
    ioreg = f'''+-o IOPlatformExpertDevice  <class IOPlatformExpertDevice>
    {{
      "IOPlatformUUID" = "{identity['uuid']}"
      "IOPlatformSerialNumber" = "{identity['serial']}"
      "manufacturer" = <"Apple Inc.">
      "model" = <"{identity['model']}">
      "board-id" = <"{board_id}">
    }}
'''
    profiler = json.dumps({'SPHardwareDataType': [{
        '_name': 'hardware_overview',
        'machine_name': identity['machine_name'],
        'machine_model': identity['model'],
        'platform_UUID': identity['uuid'],
        'serial_number': identity['serial'],
        'boot_rom_version': '',
    }]})
    failure = 'printf "%s\\n" "Unsupported hardware privacy probe" >&2\nexit 64\n'
    return {
        'ioreg': '''#!/bin/sh
case "$*" in
  '-rd1 -c IOPlatformExpertDevice'|'-c IOPlatformExpertDevice -d 2')
    /bin/cat <<'FREEBUFF_SYNTHETIC_HARDWARE'
''' + ioreg + '''FREEBUFF_SYNTHETIC_HARDWARE
    exit 0 ;;
  '-c IOPlatformDevice -d 3 -r')
    # No cluster topology is exposed; systeminformation keeps its CPU fallback.
    exit 0 ;;
esac
''' + failure,
        'system_profiler': '''#!/bin/sh
case "$*" in
  'SPHardwareDataType -json'|'-json SPHardwareDataType')
    /bin/cat <<'FREEBUFF_SYNTHETIC_HARDWARE'
''' + profiler + '''
FREEBUFF_SYNTHETIC_HARDWARE
    exit 0 ;;
esac
''' + failure,
        'hostname': f'''#!/bin/sh
case "$#:$*" in
  '0:'|'1:-f') printf '%s\\n' '{identity['hostname']}'; exit 0 ;;
  '1:-s') printf '%s\\n' '{identity['hostname'].removesuffix('.local')}'; exit 0 ;;
esac
''' + failure,
        'dmidecode': f'''#!/bin/sh
case "$*" in
  *'-t system'*|*'-t 1'*)
    /bin/cat <<'FREEBUFF_SYNTHETIC_DMI'
# dmidecode 3.3
Getting SMBIOS data from sysfs.
SMBIOS 2.8 present.

Handle 0x0001, DMI type 1, 27 bytes
System Information
	Manufacturer: {identity['machine_name']}
	Product Name: {identity['model']}
	Version: 1.0
	Serial Number: {identity['serial']}
	UUID: {identity['uuid']}
	Wake-up Type: Power Switch
	SKU Number: Default string
	Family: Notebook
FREEBUFF_SYNTHETIC_DMI
    exit 0 ;;
  *'-t 4'*|*'-t 7'*)
    exit 0 ;;
esac
''' + failure,
        'cat': f'''#!/bin/sh
for arg in "$@"; do
  case "$arg" in
    *"/etc/machine-id"|*"/var/lib/dbus/machine-id"|"machine-id")
      printf "%s\\n" "{identity['machine_id']}"
      exit 0 ;;
    *"product_uuid"*)
      printf "%s\\n" "{identity['uuid'].lower()}"
      exit 0 ;;
    *"product_serial"*)
      printf "%s\\n" "{identity['serial']}"
      exit 0 ;;
    *"product_name"*)
      printf "%s\\n" "{identity['model']}"
      exit 0 ;;
    *"product_version"*)
      printf "%s\\n" "1.0"
      exit 0 ;;
    *"sys_vendor"*)
      printf "%s\\n" "{identity['machine_name']}"
      exit 0 ;;
    *"/net/"*"/address"|*"/sys/"*"/address"|"address")
      printf "%s\\n" "{identity.get('mac_address', '02:00:00:00:00:01').lower()}"
      exit 0 ;;
  esac
done
if [ -x /bin/cat ]; then
  exec /bin/cat "$@"
elif [ -x /usr/bin/cat ]; then
  exec /usr/bin/cat "$@"
fi
exit 1
''',
    }
