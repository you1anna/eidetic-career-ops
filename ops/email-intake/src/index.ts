import { Buffer } from "node:buffer";

const RECIPIENT = "alerts@example.com";
const MAX_BYTES = 512 * 1024;
const MAX_ACK_BYTES = 4096;
const DELIVERY_TIMEOUT_MS = 10000;
class TooLarge extends Error {}
class IntakeFailure extends Error {
  constructor(readonly outcome: "configuration" | "delivery" | "acknowledgment") {
    super(`Email intake ${outcome} failed`);
  }
}
function event(outcome: string): void {
  // Fixed outcomes only: no addresses, URLs, subjects, hashes, raw content or secrets.
  console.log(JSON.stringify({ event: "email_intake", outcome }));
}

async function boundedBytes(
  stream: ReadableStream<Uint8Array>, maximum: number, signal?: AbortSignal,
): Promise<Uint8Array> {
  const reader = stream.getReader();
  const chunks: Uint8Array[] = [];
  let length = 0;
  const cancel = () => { void reader.cancel().catch(() => {}); };
  signal?.addEventListener("abort", cancel, { once: true });
  try {
    signal?.throwIfAborted();
    while (true) {
      const { done, value } = await reader.read();
      signal?.throwIfAborted();
      if (done) break;
      length += value.byteLength;
      if (length > maximum) throw new TooLarge();
      chunks.push(value);
    }
  } catch (error) {
    cancel();
    throw error;
  } finally {
    signal?.removeEventListener("abort", cancel);
    reader.releaseLock();
  }
  const bytes = new Uint8Array(length);
  let offset = 0;
  for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
  return bytes;
}
function target(env: Env): URL {
  let url: URL;
  try { url = new URL(env.INTAKE_URL); }
  catch { throw new IntakeFailure("configuration"); }
  const local = env.ALLOW_LOOPBACK_HTTP === "true" && url.protocol === "http:"
    && ["127.0.0.1", "localhost", "[::1]"].includes(url.hostname);
  if ((url.protocol !== "https:" && !local) || url.pathname !== "/v1/email"
    || url.username || url.password || url.search || url.hash
    || !env.INTAKE_TOKEN || !env.CF_ACCESS_CLIENT_ID || !env.CF_ACCESS_CLIENT_SECRET) {
    throw new IntakeFailure("configuration");
  }
  return url;
}
function utcTimestamp(value: unknown): value is string {
  if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)$/.test(value)) return false;
  const date = new Date(value);
  return Number.isFinite(date.getTime()) && date.toISOString().slice(0, 19) === value.slice(0, 19);
}
async function deliver(message: ForwardableEmailMessage, raw: Uint8Array, env: Env): Promise<boolean> {
  const url = target(env);
  const receiptId = Buffer.from(await crypto.subtle.digest("SHA-256", raw)).toString("hex");
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), DELIVERY_TIMEOUT_MS);
  try {
    const response = await fetch(url, {
      method: "POST", redirect: "manual", signal: controller.signal,
      headers: {
        "content-type": "message/rfc822",
        "authorization": `Bearer ${env.INTAKE_TOKEN}`,
        "CF-Access-Client-Id": env.CF_ACCESS_CLIENT_ID,
        "CF-Access-Client-Secret": env.CF_ACCESS_CLIENT_SECRET,
        "X-Alert-Envelope-From": message.from,
        "X-Alert-Envelope-To": message.to,
        "X-Alert-Received-At": new Date().toISOString(),
      },
      body: raw,
    });
    // Redirects must never carry credentials elsewhere, including another path on this host.
    if (![200, 201].includes(response.status)) {
      void response.body?.cancel().catch(() => {});
      throw new IntakeFailure("delivery");
    }
    if (!response.body || response.headers.get("content-type")?.split(";", 1)[0].trim().toLowerCase() !== "application/json") {
      void response.body?.cancel().catch(() => {});
      throw new IntakeFailure("acknowledgment");
    }
    let receipt: unknown;
    try {
      const bytes = await boundedBytes(response.body, MAX_ACK_BYTES, controller.signal);
      receipt = JSON.parse(new TextDecoder("utf-8", { fatal: true, ignoreBOM: false }).decode(bytes));
    } catch { throw new IntakeFailure("acknowledgment"); }
    if (typeof receipt !== "object" || receipt === null || Array.isArray(receipt)
      || !("receipt_id" in receipt) || receipt.receipt_id !== receiptId
      || !("sequence" in receipt) || !Number.isSafeInteger(receipt.sequence) || Number(receipt.sequence) < 1
      || !("received_at" in receipt) || !utcTimestamp(receipt.received_at)
      || !("duplicate" in receipt) || typeof receipt.duplicate !== "boolean") {
      throw new IntakeFailure("acknowledgment");
    }
    return receipt.duplicate;
  } catch (error) {
    if (error instanceof IntakeFailure) throw error;
    throw new IntakeFailure("delivery");
  } finally { clearTimeout(timer); }
}

export default {
  async email(message: ForwardableEmailMessage, env: Env): Promise<void> {
    if (message.to !== RECIPIENT) {
      event("recipient_rejected"); message.setReject("Recipient not accepted"); return;
    }
    if (message.from.length > 320 || /[\r\n\0]/.test(message.from)) {
      event("envelope_rejected"); message.setReject("Invalid envelope"); return;
    }
    if (message.rawSize > MAX_BYTES) {
      event("oversized"); message.setReject("Message too large"); return;
    }
    let raw: Uint8Array;
    try { raw = await boundedBytes(message.raw, MAX_BYTES); }
    catch (error) {
      if (error instanceof TooLarge) {
        event("oversized"); message.setReject("Message too large"); return;
      }
      event("stream_failed"); throw new Error("Email intake stream failed");
    }
    if (!raw.length) { event("empty"); message.setReject("Empty message"); return; }
    try {
      const duplicate = await deliver(message, raw, env);
      event(duplicate ? "duplicate" : "retained");
    } catch (error) {
      const outcome = error instanceof IntakeFailure ? error.outcome : "delivery";
      event(`${outcome}_failed`);
      throw new Error(`Email intake ${outcome} failed`);
    }
  },
  async fetch(): Promise<Response> {
    // There is no HTTP route for injecting or reading mail from this Worker.
    return new Response("Not found", { status: 404, headers: { "cache-control": "no-store" } });
  },
} satisfies ExportedHandler<Env>;
