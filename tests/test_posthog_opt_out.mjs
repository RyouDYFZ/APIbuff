// Offline regression: execute recovered analytics functions, never the CLI entrypoint.
// The VM has no network/require/timers; every SDK construction is a recording mock.
import assert from 'node:assert/strict';
import { execFileSync, spawnSync } from 'node:child_process';
import { readFileSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

const project = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const bundle = readFileSync(path.join(project, 'extracted_sources/freebuff.js'), 'utf8');
const guard = readFileSync(path.join(project, 'cli/posthog-opt-out.js'), 'utf8');

function extract(source, start, end) {
  const first = source.indexOf(start);
  assert.notEqual(first, -1, `Missing audited bundle marker: ${start}`);
  assert.equal(source.indexOf(start, first + start.length), -1, `Ambiguous marker: ${start}`);
  const last = source.indexOf(end, first + start.length);
  assert.notEqual(last, -1, `Missing end marker: ${end}`);
  return source.slice(first, last);
}

// Use the production patcher rather than reimplementing its replacement here.
// It verifies the full recovered bundle hash before returning the patched factory.
const patchedFactory = execFileSync(process.env.PYTHON_TEST_BINARY || 'python3', ['-c', `
from pathlib import Path
from scripts.build_private_cli import patch_bundle
root = Path.cwd()
patched = patch_bundle((root / 'extracted_sources/freebuff.js').read_bytes(),
                       (root / 'cli/posthog-opt-out.js').read_text())
start = patched.index('function ud(')
end = patched.index('function VvT(', start)
print(patched[start:end])
`], {
  cwd: project,
  env: { PATH: process.env.PATH, PYTHONDONTWRITEBYTECODE: '1' },
  encoding: 'utf8',
  timeout: 10_000,
}).trim();

const originalFactory = extract(bundle, 'function ud(', 'function VvT(');
const compiledEnvironment = extract(bundle, 'XuT={', '});var Xn');
const cliAnalytics = extract(bundle, 'function MET(', 'var B3A,Wh,SM,Y1');
const commonAnalytics = extract(bundle, 'function NKA(', 'var ZjT;');

function makeHarness({ flag, patched = true, runtimeEnv = {} } = {}) {
  const env = { ...runtimeEnv };
  if (flag !== undefined) env.CODEBUFF_DISABLE_POSTHOG = flag;
  const observed = { constructors: [], sdk: [], mirrors: [], anonymousIds: 0, budgets: 0 };
  const context = vm.createContext({
    process: { env },
    gJT: class RecordingPostHog {
      constructor(key, options) { observed.constructors.push({ key, options }); }
      capture(...args) { observed.sdk.push({ method: 'capture', args }); }
      identify(...args) { observed.sdk.push({ method: 'identify', args }); }
      alias(...args) { observed.sdk.push({ method: 'alias', args }); }
      captureException(...args) { observed.sdk.push({ method: 'captureException', args }); }
      async flush(...args) { observed.sdk.push({ method: 'flush', args }); }
    },
    LvT() { observed.budgets += 1; return () => {}; },
    IOT() { observed.anonymousIds += 1; return 'anon-offline-fixture'; },
    h2(record) { observed.mirrors.push(record); },
    Yn() { return true; },
    VuT() { return true; },
    AI: false,
    VOT: true,
  });
  vm.runInContext(`
    var XuT, CN, Wh, SM, Y1, MM = 'not_started', zM, Df = false, H3A, ZjT;
    ${compiledEnvironment};
    CN = XuT;
    ${guard}
    ${patched ? patchedFactory : originalFactory}
    ${cliAnalytics}
    ${commonAnalytics}
  `, context, { timeout: 1000, filename: 'recovered-analytics-offline.js' });
  return { context, observed };
}

const logger = { debug() {}, info() {}, warn() {}, error() {} };

async function exerciseBothClients(context) {
  context.SET();
  context.K_('cli.app_launched', { version: 'offline-fixture' });
  context.Zf('offline-user', { email: 'test@example.invalid' });
  context.K_('cli.fingerprint_generated', { success: true });
  context.IET(new Error('offline exception'), 'offline-user', { test: true });
  await context.z2();
  context.NKA({ event: 'agent.offline_event', userId: 'offline-user', properties: {}, logger });
}

for (const flag of ['1', 'true']) {
  test(`runtime opt-out ${JSON.stringify(flag)} prevents both SDK clients and all SDK operations`, async () => {
    const { context, observed } = makeHarness({ flag });
    await exerciseBothClients(context);
    assert.equal(observed.constructors.length, 0);
    assert.equal(observed.budgets, 0, 'No exception hooks should be configured');
    assert.deepEqual(observed.sdk, []);
    assert.equal(context.MM, 'ready', 'The CLI can continue after opting out');
    assert.equal(context.Df, true, 'Local authenticated state remains usable');
    assert.equal(Object.isFrozen(context.Y1), true);
    assert.equal(observed.mirrors.length, 2, 'Axiom mirroring remains an independent control');
  });

  test(`startup opt-out ${JSON.stringify(flag)} survives later environment changes`, async () => {
    const { context, observed } = makeHarness({ flag });
    context.process.env.CODEBUFF_DISABLE_POSTHOG = '';
    await exerciseBothClients(context);
    assert.equal(observed.constructors.length, 0);
    assert.equal(observed.budgets, 0);
    assert.deepEqual(observed.sdk, []);
    assert.equal(context.gatewayPostHogDisabled(), true);
  });
}

for (const flag of [undefined, '', 'false', '0']) {
  test(`opt-out ${JSON.stringify(flag)} preserves normal analytics behavior`, async () => {
    const { context, observed } = makeHarness({ flag });
    await exerciseBothClients(context);
    assert.equal(observed.constructors.length, 2, 'CLI and SDK/common each use the shared factory');
    assert.equal(observed.budgets, 2);
    assert.deepEqual(observed.sdk.map(call => call.method), [
      'capture', 'alias', 'identify', 'capture', 'captureException', 'flush', 'capture',
    ]);
    assert.equal(observed.constructors[0].options.enableExceptionAutocapture, true);
    assert.equal(observed.constructors[1].options.flushAt, 1);
    assert.equal(observed.constructors[1].options.flushInterval, 0);
    assert.equal(observed.sdk[0].args[0].distinctId, 'anon-offline-fixture');
    assert.equal(observed.sdk[3].args[0].distinctId, 'offline-user');
  });
}

test('NEXT_PUBLIC runtime overrides do not replace the official compiled PostHog constants', () => {
  const runtimeEnv = {
    NEXT_PUBLIC_POSTHOG_API_KEY: '',
    NEXT_PUBLIC_POSTHOG_HOST_URL: 'http://127.0.0.1:9',
  };
  const { context, observed } = makeHarness({ patched: false, runtimeEnv });
  assert.notEqual(context.CN.NEXT_PUBLIC_POSTHOG_API_KEY, runtimeEnv.NEXT_PUBLIC_POSTHOG_API_KEY);
  assert.equal(context.CN.NEXT_PUBLIC_POSTHOG_HOST_URL, 'https://us.i.posthog.com');
  context.SET();
  assert.equal(observed.constructors.length, 1);
  assert.equal(observed.constructors[0].options.host, 'https://us.i.posthog.com');
});

test('failed CLI initialization still permits analytics mirrors and local identification', async () => {
  const { context, observed } = makeHarness();
  // Dependency injection bypasses the common module's earlier schema validation.
  context.T69({ env: { NEXT_PUBLIC_POSTHOG_API_KEY: '', NEXT_PUBLIC_POSTHOG_HOST_URL: 'http://127.0.0.1:9' } });
  assert.throws(() => context.SET(), /POSTHOG_API_KEY or NEXT_PUBLIC_POSTHOG_HOST_URL is not set/);
  assert.equal(context.MM, 'failed');
  assert.equal(context.K_('cli.app_launched', {}), true);
  context.Zf('offline-user', {});
  context.IET(new Error('offline exception'));
  await context.z2();
  assert.equal(observed.constructors.length, 0);
  assert.deepEqual(observed.sdk, []);
  assert.equal(observed.anonymousIds, 1);
  assert.equal(observed.mirrors.length, 1);
  assert.equal(observed.mirrors[0].client_session_id, 'anon-offline-fixture');
  assert.equal(context.SM, 'offline-user');
});

test('Bun standalone build reads the opt-out at runtime instead of inlining the build environment', { timeout: 45_000 }, t => {
  const bun = process.env.BUN_TEST_BINARY || 'bun';
  const available = spawnSync(bun, ['--version'], {
    env: { PATH: process.env.PATH }, encoding: 'utf8', timeout: 5000,
  });
  if (available.error?.code === 'ENOENT') {
    t.skip('Bun is unavailable; run again on the CLI build host');
    return;
  }
  assert.equal(available.status, 0, available.stderr);
  const temporary = mkdtempSync(path.join(tmpdir(), 'posthog-guard-compile-'));
  try {
    const source = path.join(temporary, 'guard.js');
    const binary = path.join(temporary, process.platform === 'win32' ? 'guard.exe' : 'guard');
    writeFileSync(source, `${guard}\nconsole.log(JSON.stringify({ disabled: gatewayPostHogDisabled() }));\n`);
    const compilerTemporary = path.join(temporary, 'compiler-tmp');
    mkdirSync(compilerTemporary);
    const baseEnv = { PATH: process.env.PATH, HOME: temporary, TMPDIR: compilerTemporary };
    execFileSync(bun, ['build', '--compile', '--env=disable',
      '--no-compile-autoload-dotenv', '--no-compile-autoload-bunfig',
      '--no-compile-autoload-tsconfig', '--no-compile-autoload-package-json',
      '--outfile', binary, source], {
      cwd: temporary, env: { ...baseEnv, CODEBUFF_DISABLE_POSTHOG: 'false' },
      encoding: 'utf8', timeout: 30_000,
    });
    for (const flag of [undefined, '', 'false', '1', 'true']) {
      const env = { ...baseEnv };
      if (flag !== undefined) env.CODEBUFF_DISABLE_POSTHOG = flag;
      const output = execFileSync(binary, [], { cwd: temporary, env, encoding: 'utf8', timeout: 5000 });
      assert.deepEqual(JSON.parse(output), { disabled: flag === '1' || flag === 'true' });
    }
  } finally {
    rmSync(temporary, { recursive: true, force: true });
  }
});
