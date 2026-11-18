// fetch rejects with a TypeError only when no response arrived: the connection dropped or a browser
// extension blocked it. Browsers word that differently ("Failed to fetch", "Load failed"), so say what
// to do instead. It must read on its own and after a prefix such as "Not saved: ".
export const UNREACHABLE =
  "The site could not be reached. Check your connection and try again. If it still fails, copy anything you typed, reload the page (sign in again if asked) and check that an ad blocker is not blocking this site.";

// Cloudflare Access answers every request with a redirect to its login page once the one-week
// sign-in ends. The site itself never redirects /api, so a redirect here means signed out.
export const SIGNED_OUT =
  "Your sign-in has ended, so this was not saved. Sign in again in a new tab, then come back and try again: what you typed here is kept.";

const signedOutHandlers = new Set();
export function onSignedOut(handler) {
  signedOutHandlers.add(handler);
  return () => signedOutHandlers.delete(handler);
}

export async function api(path, options) {
  let res;
  try {
    // Following the redirect would run into the page's connect-src policy, which blocks it as an
    // unexplained "Failed to fetch"; not following it returns an opaque redirect instead.
    res = await fetch(path, { ...options, redirect: "manual" });
  } catch (error) {
    if (error instanceof TypeError) throw new Error(UNREACHABLE, { cause: error });
    throw error;
  }
  if (res.type === "opaqueredirect" || (res.status >= 300 && res.status < 400)) {
    signedOutHandlers.forEach(handler => handler());
    throw Object.assign(new Error(SIGNED_OUT), { signedOut: true });
  }
  let body = {};
  try {
    body = await res.json();
  } catch {}
  if (res.status === 409)
    throw new Error((path === "/api/jobs/save" || path.startsWith("/api/incoming/"))
      ? body.error || "This job cannot be saved. Review the existing record."
      : "This record changed elsewhere. Refresh and try again.");
  if (!res.ok)
    throw new Error(
      body.error || body.message || `Request failed (${res.status})`,
    );
  return body;
}
