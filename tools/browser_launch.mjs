// One launcher for Playwright. On macOS, WebKit's app calls NSApplicationMain as it starts.
// Inside an agent seatbelt that call abort()s, and macOS writes a crash report for each helper.
// Cursor sets CURSOR_SANDBOX=seatbelt. Codex sets CODEX_SANDBOX=seatbelt. Neither may launch a browser.

export function seatbeltSandbox(platform = process.platform, env = process.env) {
  if (platform !== 'darwin') return false;
  return env.CURSOR_SANDBOX === 'seatbelt' || env.CODEX_SANDBOX === 'seatbelt';
}

export async function launchBrowser(type, options = {}) {
  if (seatbeltSandbox()) {
    throw new Error(
      'Browser was not started: this process is inside an agent command sandbox. ' +
      'WebKit aborts while registering as a macOS application and leaves crash reports. ' +
      'Re-run outside the sandbox through the client\'s normal permission request.');
  }
  return type.launch(options);
}
