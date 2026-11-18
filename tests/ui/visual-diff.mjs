// One pixel-comparison owner for the small browser visual suite. Never updates references.
import { closeSync, existsSync, mkdirSync, openSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { randomUUID } from 'node:crypto';
import { isDeepStrictEqual } from 'node:util';
import pixelmatch from 'pixelmatch';
import { PNG } from 'pngjs';

export const comparison = Object.freeze({ threshold: 0.1, includeAA: false });

export function comparePng(expected, actual) {
  const before = PNG.sync.read(expected), after = PNG.sync.read(actual);
  if (before.width !== after.width || before.height !== after.height)
    throw new Error(`Screenshot dimensions differ: expected ${before.width}×${before.height}, actual ${after.width}×${after.height}`);
  const diff = new PNG({ width: before.width, height: before.height });
  const changedPixels = pixelmatch(before.data, after.data, diff.data, before.width, before.height, comparison);
  return { changedPixels, diff: PNG.sync.write(diff) };
}

export function assertEnvironment(expected, actual, { replaceAll = false } = {}) {
  if (!isDeepStrictEqual(expected, actual) && !replaceAll)
    throw new Error('Visual baseline environment differs. Use its recorded environment, or run --update without --state to replace and review the complete set.\n'
      + `Expected: ${JSON.stringify(expected)}\nActual: ${JSON.stringify(actual)}`);
}

// One invocation at a time: a check must never read a half-updated reference set.
// Refuse promptly. The token prevents late cleanup from unlinking a replacement lock.
export function lockBaselines(directory) {
  mkdirSync(directory, { recursive: true });
  const path = join(directory, '.run.lock');
  let descriptor;
  try { descriptor = openSync(path, 'wx', 0o600); }
  catch (error) {
    if (error.code === 'EEXIST') throw new Error(`Visual baselines are busy: ${path}. Wait for the current run; remove a stale lock only after confirming no visual check is running.`);
    throw error;
  }
  const owner = `${process.pid}\n${randomUUID()}\n`;
  try { writeFileSync(descriptor, owner); }
  catch (error) { try { rmSync(path, { force: true }); } catch {} throw error; }
  finally { closeSync(descriptor); }
  let released = false;
  return () => {
    if (released) return;
    released = true;
    try { if (readFileSync(path, 'utf8') === owner) rmSync(path); }
    catch (error) { if (error.code !== 'ENOENT') throw error; }
  };
}

export function assertVisualBaseline({ actual, baselinePath, artifactDir, name }) {
  if (!/^[a-z0-9-]+$/.test(name)) throw new Error('Invalid visual state name');
  if (!existsSync(baselinePath)) throw new Error(`Missing visual baseline: ${baselinePath}. Generate with --update, inspect it, then commit the reviewed image.`);
  const expected = readFileSync(baselinePath);
  let result, failure;
  try {
    result = comparePng(expected, actual);
    if (result.changedPixels) failure = `${result.changedPixels} pixels changed`;
  } catch (error) { failure = error.message; }
  if (!failure) return;
  let evidence = `evidence: ${artifactDir}`;
  try {
    mkdirSync(artifactDir, { recursive: true, mode: 0o700 });
    for (const [kind, image] of [['expected', expected], ['actual', actual], ['diff', result?.diff]])
      if (image) writeFileSync(join(artifactDir, `${name}-${kind}.png`), image, { mode: 0o600 });
  } catch (error) { evidence = `could not save evidence: ${error.message}`; }
  throw new Error(`Visual mismatch (${name}): ${failure}; ${evidence}`);
}
