import assert from 'node:assert/strict';
import { test } from 'node:test';
import { mkdtempSync, readFileSync, existsSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { PNG } from 'pngjs';
import { comparePng, assertVisualBaseline, assertEnvironment, lockBaselines } from './visual-diff.mjs';

function image(width = 12, height = 12, changed = false) {
  const png = new PNG({ width, height });
  png.data.fill(255);
  if (changed) for (let y = 4; y < 8; y++) for (let x = 4; x < 8; x++) {
    const offset = (y * width + x) * 4;
    png.data[offset] = png.data[offset + 1] = png.data[offset + 2] = 0;
  }
  return PNG.sync.write(png);
}

test('unchanged pixels pass; a deliberate 4×4 solid change fails; restoring passes', () => {
  const original = image();
  assert.equal(comparePng(original, original).changedPixels, 0);
  assert.equal(comparePng(original, image(12, 12, true)).changedPixels, 16);
  assert.equal(comparePng(original, image()).changedPixels, 0);
});

test('a changed viewport cannot pass pixel comparison', () => {
  assert.throws(() => comparePng(image(), image(13, 12)), /dimensions/);
});

test('missing and changed references fail without creating or replacing the baseline', () => {
  const dir = mkdtempSync(join(tmpdir(), 'wo-visual-test-'));
  try {
    const baselinePath = join(dir, 'baseline.png'), artifactDir = join(dir, 'artifacts');
    const args = { baselinePath, artifactDir, name: 'week', actual: image() };
    assert.throws(() => assertVisualBaseline(args), /Missing visual baseline/);
    assert.equal(existsSync(baselinePath), false);
    writeFileSync(baselinePath, image());
    assert.throws(() => assertVisualBaseline({ ...args, actual: image(12, 12, true) }), /16 pixels/);
    assert.deepEqual(readFileSync(baselinePath), image());
    for (const file of ['week-expected.png', 'week-actual.png', 'week-diff.png'])
      assert.ok(existsSync(join(artifactDir, file)), file);
    assertVisualBaseline(args);
  } finally { rmSync(dir, { recursive: true, force: true }); }
});

test('environment changes are refused even when pixels would happen to match', () => {
  const expected = { os: 'darwin-27', browser: 'chromium-100', viewport: { width: 1300, height: 900 } };
  assert.doesNotThrow(() => assertEnvironment(expected, structuredClone(expected)));
  assert.throws(() => assertEnvironment(expected, { ...expected, os: 'linux' }), /environment differs/);
  assert.throws(() => assertEnvironment(expected, { ...expected, browser: 'chromium-101' }), /environment differs/);
  assert.doesNotThrow(() => assertEnvironment(expected, { ...expected, browser: 'chromium-101' }, { replaceAll: true }));
});

test('artifact names cannot escape the run directory', () => {
  assert.throws(() => assertVisualBaseline({ name: '../escape', actual: image() }), /Invalid visual state/);
});

test('an unwritable artifact destination preserves the original pixel mismatch', () => {
  const dir = mkdtempSync(join(tmpdir(), 'wo-visual-capture-test-'));
  try {
    const baselinePath = join(dir, 'baseline.png'), artifactDir = join(dir, 'existing-file');
    writeFileSync(baselinePath, image()); writeFileSync(artifactDir, 'not a directory');
    assert.throws(() => assertVisualBaseline({ actual: image(12, 12, true), baselinePath, artifactDir, name: 'week' }),
      /Visual mismatch \(week\): 16 pixels changed.*could not save evidence/s);
  } finally { rmSync(dir, { recursive: true, force: true }); }
});

test('overlapping checks or baseline updates are refused; release permits the next run', () => {
  const dir = mkdtempSync(join(tmpdir(), 'wo-visual-lock-test-'));
  try {
    const release = lockBaselines(dir);
    assert.throws(() => lockBaselines(dir), /Visual baselines are busy/);
    release();
    lockBaselines(dir)();
  } finally { rmSync(dir, { recursive: true, force: true }); }
});

test('a late or repeated unlock cannot remove another invocation lock', () => {
  const dir = mkdtempSync(join(tmpdir(), 'wo-visual-lock-owner-test-'));
  try {
    const first = lockBaselines(dir);
    first();
    const second = lockBaselines(dir);
    first();
    assert.throws(() => lockBaselines(dir), /Visual baselines are busy/);
    second();
    const stale = lockBaselines(dir);
    rmSync(join(dir, '.run.lock'));
    const replacement = lockBaselines(dir);
    stale();
    assert.throws(() => lockBaselines(dir), /Visual baselines are busy/);
    replacement();
  } finally { rmSync(dir, { recursive: true, force: true }); }
});
