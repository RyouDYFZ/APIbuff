'use strict'

// Loaded before the compiled CLI's imports. All values are synthetic and scoped
// to this worker. Native programs remain subject to the launch sandbox.
const fs = require('node:fs')
const path = require('node:path')
const os = require('node:os')
const cp = require('node:child_process')
const { syncBuiltinESMExports } = require('node:module')
const { promisify } = require('node:util')
const identity = JSON.parse(fs.readFileSync(path.join(__dirname, 'identity.json'), 'utf8'))
if (!/^[A-F0-9-]{36}$/i.test(identity.uuid) ||
    !/^[A-HJ-NP-Z0-9]{12}$/.test(identity.serial) ||
    (identity.machine_id && !/^[a-f0-9]{32}$/.test(identity.machine_id)) ||
    (identity.mac_address && !/^([0-9a-f]{2}:){5}[0-9a-f]{2}$/i.test(identity.mac_address)) ||
    !/^[a-z0-9][a-z0-9-]{0,30}\.local$/.test(identity.hostname)) {
  throw new Error('Invalid worker hardware privacy profile')
}
if (!identity.machine_id) {
  identity.machine_id = identity.uuid.replace(/-/g, '').toLowerCase()
}
if (!identity.mac_address) {
  const hash = require('node:crypto').createHash('sha256').update(identity.uuid).digest('hex')
  identity.mac_address = `02:${hash.slice(2, 4)}:${hash.slice(4, 6)}:${hash.slice(6, 8)}:${hash.slice(8, 10)}:${hash.slice(10, 12)}`.toLowerCase()
}

os.hostname = () => identity.hostname
process.env.HOSTNAME = identity.hostname
process.env.HOST = identity.hostname

// Virtualize MAC address via os.networkInterfaces
const primaryIf = process.platform === 'darwin' ? 'en0' : 'eth0'
const syntheticMac = identity.mac_address.toLowerCase()
os.networkInterfaces = function () {
  return {
    lo0: [{
      address: '127.0.0.1',
      netmask: '255.0.0.0',
      family: 'IPv4',
      mac: '00:00:00:00:00:00',
      internal: true,
      cidr: '127.0.0.1/8'
    }],
    [primaryIf]: [{
      address: '192.168.1.100',
      netmask: '255.255.255.0',
      family: 'IPv4',
      mac: syntheticMac,
      internal: false,
      cidr: '192.168.1.100/24'
    }]
  }
}

// Virtualize Linux hardware features via Node fs module for JS consumers
const SYNTHETIC_MAP = {
  '/etc/machine-id': () => identity.machine_id,
  '/var/lib/dbus/machine-id': () => identity.machine_id,
  'machine-id': () => identity.machine_id,
  'product_uuid': () => identity.uuid.toLowerCase(),
  'product_serial': () => identity.serial,
  'product_name': () => 'Virtual Machine',
  'product_version': () => '1.0',
  'sys_vendor': () => 'Cloud Provider',
}

function matchSynthetic(targetPath) {
  if (typeof targetPath !== 'string') return null
  if ((targetPath.endsWith('/address') || targetPath.endsWith('address')) &&
      (targetPath.includes('/net/') || targetPath.includes('/sys/'))) {
    return identity.mac_address.toLowerCase()
  }
  for (const [key, getter] of Object.entries(SYNTHETIC_MAP)) {
    if (targetPath === key || targetPath.endsWith('/' + key)) {
      return getter()
    }
  }
  return null
}

const originalReadFileSync = fs.readFileSync
fs.readFileSync = function (targetPath, options) {
  const synth = matchSynthetic(targetPath)
  if (synth !== null) {
    const encoding = typeof options === 'string' ? options : (options && options.encoding)
    return encoding ? synth + '\n' : Buffer.from(synth + '\n')
  }
  return Reflect.apply(originalReadFileSync, this, arguments)
}

const originalReadFile = fs.readFile
fs.readFile = function (targetPath, ...args) {
  const synth = matchSynthetic(targetPath)
  if (synth !== null) {
    const cb = typeof args[args.length - 1] === 'function' ? args[args.length - 1] : null
    const options = typeof args[0] !== 'function' ? args[0] : null
    const encoding = typeof options === 'string' ? options : (options && options.encoding)
    const data = encoding ? synth + '\n' : Buffer.from(synth + '\n')
    if (cb) {
      process.nextTick(() => cb(null, data))
      return
    }
  }
  return Reflect.apply(originalReadFile, this, args)
}

if (fs.promises && fs.promises.readFile) {
  const originalPromisesReadFile = fs.promises.readFile
  fs.promises.readFile = async function (targetPath, options) {
    const synth = matchSynthetic(targetPath)
    if (synth !== null) {
      const encoding = typeof options === 'string' ? options : (options && options.encoding)
      return encoding ? synth + '\n' : Buffer.from(synth + '\n')
    }
    return Reflect.apply(originalPromisesReadFile, this, arguments)
  }
}

// Virtualize proxy environment variables so that Freebuff CLI's client-environment
// descriptor observes a remote proxy (px=remote) instead of local loopback (px=loopback).
// Low-level C++ socket calls continue to use the process environment's actual loopback IP.
const PROXY_KEYS = new Set([
  'HTTP_PROXY', 'http_proxy',
  'HTTPS_PROXY', 'https_proxy',
  'ALL_PROXY', 'all_proxy'
])
const LOOPBACK_PROXY_RE = /^(?:https?|socks5?h?):\/\/(?:127(?:\.\d{1,3}){3}|localhost|\[?::1\]?|0\.0\.0\.0)(?::(\d+))?\/?$/i

try {
  const originalEnv = process.env
  const envProxy = new Proxy(originalEnv, {
    get(target, prop) {
      if (typeof prop === 'string' && PROXY_KEYS.has(prop)) {
        const val = target[prop]
        if (typeof val === 'string' && val.trim()) {
          const match = val.trim().match(LOOPBACK_PROXY_RE)
          if (match) {
            const port = match[1] ? `:${match[1]}` : ''
            return `http://proxy.internal${port}`
          }
        }
      }
      return target[prop]
    }
  })
  Object.defineProperty(process, 'env', {
    value: envProxy,
    configurable: true,
    writable: true,
    enumerable: true
  })
} catch (_) {}

// Universal dynamic PostHog telemetry suppression (Defense-in-depth).
// Drops any outgoing telemetry network request to PostHog domains and returns
// dummy 200 OK so the analytics queue drains without error or leak.
try {
  const https = require('node:https')
  const http = require('node:http')
  const isPostHogUrl = (target) => {
    if (!target) return false
    const s = String(target).toLowerCase()
    return s.includes('posthog.com') || s.includes('posthog')
  }

  function interceptModuleRequest(mod) {
    const originalRequest = mod.request
    mod.request = function (...args) {
      const first = args[0]
      const url = typeof first === 'string' ? first : (first?.hostname || first?.host || first?.href || '')
      if (isPostHogUrl(url)) {
        const { EventEmitter } = require('node:events')
        const req = new EventEmitter()
        req.write = () => true
        req.end = () => {
          const res = new EventEmitter()
          res.statusCode = 200
          res.headers = { 'content-type': 'application/json' }
          const cb = typeof args[args.length - 1] === 'function' ? args[args.length - 1] : null
          if (cb) cb(res)
          res.emit('data', Buffer.from(JSON.stringify({ status: 1 })))
          res.emit('end')
        }
        return req
      }
      return Reflect.apply(originalRequest, this, args)
    }
  }

  interceptModuleRequest(https)
  interceptModuleRequest(http)

  if (typeof globalThis.fetch === 'function') {
    const originalFetch = globalThis.fetch
    globalThis.fetch = async function (...args) {
      const first = args[0]
      const url = typeof first === 'string' ? first : (first?.url || '')
      if (isPostHogUrl(url)) {
        return new Response(JSON.stringify({ status: 1 }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' }
        })
      }
      return Reflect.apply(originalFetch, this, args)
    }
  }
} catch (_) {}

const names = new Set(['ioreg', 'system_profiler', 'hostname', 'dmidecode'])
const quote = value => "'" + value.replace(/'/g, "'\\''") + "'"
const shim = name => path.join(__dirname, 'fake_bin', name)
function redirectFile(file) {
  if (typeof file !== 'string') return file
  const name = path.basename(file)
  if (names.has(name) && (file === name ||
      ['/usr/sbin', '/usr/bin', '/bin', '/sbin'].includes(path.dirname(file)))) return shim(name)
  return file
}
function redirectCommand(command) {
  if (typeof command !== 'string') return command
  // Only a command in the initial executable position is rewritten. Shell
  // constructs/obfuscated paths are blocked by sandbox-exec on macOS.
  return command.replace(/^(\s*)(\/(?:usr\/sbin|usr\/bin|bin|sbin)\/(?:ioreg|system_profiler|hostname|dmidecode))(?=\s|$)/,
    (_match, space, file) => space + quote(redirectFile(file)))
}
for (const method of ['exec', 'execSync']) {
  const original = cp[method]
  cp[method] = function (command, ...args) {
    return Reflect.apply(original, this, [redirectCommand(command), ...args])
  }
}
for (const method of ['execFile', 'execFileSync', 'spawn', 'spawnSync']) {
  const original = cp[method]
  cp[method] = function (file, ...args) {
    return Reflect.apply(original, this, [redirectFile(file), ...args])
  }
}
// Preserve Node's special exec promisification contract ({stdout, stderr}
// and promise.child). Copying the original custom function would bypass us.
for (const method of ['exec', 'execFile']) {
  cp[method][promisify.custom] = (...args) => {
    let child
    const promise = new Promise((resolve, reject) => {
      child = cp[method](...args, (error, stdout, stderr) => {
        if (error) {
          error.stdout = stdout
          error.stderr = stderr
          reject(error)
        } else resolve({ stdout, stderr })
      })
    })
    promise.child = child
    return promise
  }
}
syncBuiltinESMExports()

// This probe runs in the actual standalone executable, then exits before its
// application entrypoint.
if (process.env.FREEBUFF_DEVICE_PROBE === '1') {
  try {
    const assert = require('node:assert/strict')
    const importedOS = require('node:os')
    const importedCP = require('node:child_process')
    assert.equal(importedOS.hostname(), identity.hostname)

    if (process.platform === 'darwin') {
      for (const command of ['ioreg -rd1 -c IOPlatformExpertDevice',
        'ioreg -c IOPlatformExpertDevice -d 2',
        '/usr/sbin/ioreg -rd1 -c IOPlatformExpertDevice',
        '/usr/sbin/ioreg -c IOPlatformExpertDevice -d 2']) {
        const output = importedCP.execSync(command, { encoding: 'utf8' })
        assert(output.includes(identity.uuid))
        assert(output.includes(identity.serial))
      }
      const hardware = JSON.parse(cp.execFileSync('/usr/sbin/system_profiler',
        ['SPHardwareDataType', '-json'], { encoding: 'utf8' })).SPHardwareDataType[0]
      assert.equal(hardware.platform_UUID, identity.uuid)
      assert.equal(hardware.serial_number, identity.serial)
      assert.equal(cp.execFileSync('hostname', [], { encoding: 'utf8' }).trim(), identity.hostname)
      // Bypass the JS command rewrite and PATH deliberately. The OS must deny it.
      for (const command of ['/usr/sbin/ioreg -rd1 -c IOPlatformExpertDevice',
        '/usr/sbin/system_profiler SPHardwareDataType -json', '/bin/hostname']) {
        const result = cp.spawnSync('/bin/sh', ['-c', command], { encoding: 'utf8' })
        assert.notEqual(result.status, 0)
        assert.equal(result.stdout, '')
      }
    } else {
      // Linux probes:
      assert.equal(cp.execFileSync('hostname', [], { encoding: 'utf8' }).trim(), identity.hostname)
      const dmiOut = importedCP.execSync('dmidecode -t system', { encoding: 'utf8' })
      assert(dmiOut.includes(identity.uuid))
      assert(dmiOut.includes(identity.serial))

      const machineIdOut = importedCP.execSync('cat /etc/machine-id', { encoding: 'utf8' }).trim()
      assert.equal(machineIdOut, identity.machine_id)

      const uuidOut = importedCP.execSync('cat /sys/class/dmi/id/product_uuid', { encoding: 'utf8' }).trim()
      assert.equal(uuidOut.toLowerCase(), identity.uuid.toLowerCase())

      const fsMachineId = fs.readFileSync('/etc/machine-id', 'utf8').trim()
      assert.equal(fsMachineId, identity.machine_id)

      const fsUuid = fs.readFileSync('/sys/class/dmi/id/product_uuid', 'utf8').trim()
      assert.equal(fsUuid.toLowerCase(), identity.uuid.toLowerCase())

      const macOut = importedCP.execSync('cat /sys/class/net/eth0/address', { encoding: 'utf8' }).trim()
      assert.equal(macOut.toLowerCase(), identity.mac_address.toLowerCase())

      const fsMac = fs.readFileSync('/sys/class/net/eth0/address', 'utf8').trim()
      assert.equal(fsMac.toLowerCase(), identity.mac_address.toLowerCase())
    }

    const probeIfaces = importedOS.networkInterfaces()
    const probeMacs = Object.values(probeIfaces).flat().map(i => i.mac).filter(m => m && m !== '00:00:00:00:00:00')
    assert(probeMacs.includes(identity.mac_address.toLowerCase()))

    process.stdout.write('FREEBUFF_DEVICE_PROBE_OK_v1\n')
    process.exit(0)
  } catch (err) {
    process.stderr.write('Worker hardware privacy probe failed: ' + (err ? (err.stack || err.message) : 'unknown') + '\n')
    process.exit(78)
  }
}
