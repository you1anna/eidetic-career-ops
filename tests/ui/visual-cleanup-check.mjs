// Real child-process cancellation contract. Uses synthetic Chromium only, serially.
import assert from 'node:assert/strict';
import { spawn, execFileSync } from 'node:child_process';
import { existsSync, mkdtempSync, readFileSync, readdirSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const lock = join(here, 'visual-baselines/.run.lock');
let activeChild, stopped;
function interrupt(signal) { stopped ||= signal; activeChild?.kill(signal); }
const onInterrupt = () => interrupt('SIGINT'), onTerminate = () => interrupt('SIGTERM');
process.on('SIGINT', onInterrupt); process.on('SIGTERM', onTerminate);
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
const children = pid => execFileSync('ps', ['-axo', 'pid=,ppid='], { encoding: 'utf8' })
  .trim().split('\n').map(line => line.trim().split(/\s+/).map(Number)).filter(([, parent]) => parent === pid).map(([child]) => child);
function running(pid) { try { process.kill(pid, 0); return true; } catch { return false; } }

async function main() {
  assert.equal(existsSync(lock), false, 'run cleanup checks only while visual comparisons are idle');
  for (const [signal, expected] of [['SIGINT', 130], ['SIGTERM', 143]]) {
    const root = mkdtempSync(join(tmpdir(), 'wo-visual-interrupt-check-'));
    const child = spawn(process.execPath, [join(here, 'visual-regression.mjs')], {
      env: { ...process.env, TMPDIR: root, WO_UI_ARTIFACT_DIR: root, WO_UI_BROWSERS: 'chromium' },
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    activeChild = child;
    let output = '', descendants = [];
    child.stdout.on('data', bytes => { output += bytes; });
    child.stderr.on('data', bytes => { output += bytes; });
    const exited = new Promise((resolve, reject) => { child.once('exit', (code, caughtSignal) => resolve({ code, signal: caughtSignal })); child.once('error', reject); });
    try {
      const deadline = Date.now() + 15000;
      while (!stopped && !output.includes('ok visual chromium: this-week') && child.exitCode === null && child.signalCode === null && Date.now() < deadline) await pause(10);
      descendants = children(child.pid);
      if (stopped) return;
      assert.match(output, /ok visual chromium: this-week/, output);
      assert.ok(readdirSync(root).some(name => name.startsWith('wo-visual-fixture-')), 'fixture exists before interruption');
      assert.ok(descendants.length > 0, 'fixture server exists before interruption');
      child.kill(signal);
      const timeout = setTimeout(() => child.kill('SIGKILL'), 3000);
      let result;
      try { result = await exited; } finally { clearTimeout(timeout); }
      if (stopped) return;
      assert.deepEqual(result, { code: expected, signal: null }, output);
      assert.equal(existsSync(lock), false, 'interruption releases baseline lock');
      assert.deepEqual(readdirSync(root), [], 'interruption removes owned temporary data and captures');
      assert.deepEqual(descendants.filter(running), [], 'interruption stops owned child processes');
      console.log(`ok visual cleanup ${signal}: exit ${expected}, no lock, temporary files or child processes`);
    } finally {
      if (child.exitCode === null && child.signalCode === null)
        descendants = [...new Set([...descendants, ...children(child.pid)])];
      const timeout = setTimeout(() => child.kill('SIGKILL'), 3000);
      if (!stopped && child.exitCode === null && child.signalCode === null) child.kill('SIGKILL');
      try { await exited; } finally { clearTimeout(timeout); }
      for (const pid of descendants) if (running(pid)) { try { process.kill(pid, 'SIGKILL'); } catch {} }
      if (existsSync(lock) && readFileSync(lock, 'utf8').split('\n')[0] === String(child.pid)) rmSync(lock);
      rmSync(root, { recursive: true, force: true });
      activeChild = undefined;
    }
  }

  const root = mkdtempSync(join(tmpdir(), 'wo-evidence-launch-check-'));
  try {
    const child = spawn(process.execPath, [join(here, 'browser-evidence-check.mjs')], {
      env: { ...process.env, TMPDIR: root, WO_UI_BROWSERS: 'chromium', WO_UI_CHROMIUM: join(root, 'missing-browser') },
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    activeChild = child;
    let output = '';
    child.stdout.on('data', bytes => { output += bytes; }); child.stderr.on('data', bytes => { output += bytes; });
    const code = await new Promise((resolve, reject) => { child.once('exit', resolve); child.once('error', reject); });
    if (stopped) return;
    assert.equal(code, 1, output);
    assert.match(output, /executable doesn't exist/);
    assert.deepEqual(readdirSync(root), [], 'failed browser launch removes the self-check temporary directory');
    console.log('ok browser evidence launch failure: original error and no temporary files');
  } finally { rmSync(root, { recursive: true, force: true }); activeChild = undefined; }
}
try { await main(); }
finally {
  process.off('SIGINT', onInterrupt); process.off('SIGTERM', onTerminate);
  if (stopped) process.exitCode = stopped === 'SIGINT' ? 130 : 143;
}
