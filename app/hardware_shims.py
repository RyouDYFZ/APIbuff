"""Synthetic outputs for the macOS and Linux probes used by the extracted CLI."""

import hashlib
import json
import re
import uuid


def hardware_identity(account_id):
    if not isinstance(account_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', account_id):
        raise ValueError('A valid account ID is required for hardware privacy.')
    return {
        'uuid': str(uuid.uuid5(uuid.NAMESPACE_URL, 'freebuff:hardware:v1:' + account_id)).upper(),
        'serial': hashlib.sha256(('freebuff:serial:v1:' + account_id).encode()).hexdigest()[:12].upper(),
        'hostname': 'developer-workstation.local',
        'machine_id': hashlib.sha256(('freebuff:machineid:v1:' + account_id).encode()).hexdigest()[:32].lower(),
    }


def shim_sources(identity):
    """No shim executes a real probe, even for an unrecognized argument list."""
    ioreg = f'''+-o IOPlatformExpertDevice  <class IOPlatformExpertDevice>
    {{
      "IOPlatformUUID" = "{identity['uuid']}"
      "IOPlatformSerialNumber" = "{identity['serial']}"
      "manufacturer" = <"Apple Inc.">
      "model" = <"VirtualMac1,1">
      "board-id" = <"VirtualMac">
    }}
'''
    profiler = json.dumps({'SPHardwareDataType': [{
        '_name': 'hardware_overview',
        'machine_name': 'Virtual Workstation',
        'machine_model': 'VirtualMac1,1',
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
        'hostname': '''#!/bin/sh
case "$#:$*" in
  '0:'|'1:-f') printf '%s\\n' 'developer-workstation.local'; exit 0 ;;
  '1:-s') printf '%s\\n' 'developer-workstation'; exit 0 ;;
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
	Manufacturer: Cloud Provider
	Product Name: Virtual Machine
	Version: 1.0
	Serial Number: {identity['serial']}
	UUID: {identity['uuid']}
	Wake-up Type: Power Switch
	SKU Number: Default string
	Family: Virtual
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
      printf "%s\\n" "Virtual Machine"
      exit 0 ;;
    *"product_version"*)
      printf "%s\\n" "1.0"
      exit 0 ;;
    *"sys_vendor"*)
      printf "%s\\n" "Cloud Provider"
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
