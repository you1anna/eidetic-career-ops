import { env, exports } from "cloudflare:workers";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { Buffer } from "node:buffer";
import worker from "../src/index";

const raw = "From: jobalerts-noreply@linkedin.com\r\nSubject: synthetic alert\r\n\r\nPrivate test body";
function mail(content = raw, to = "alerts@example.com", rawSize?: number) {
  const bytes = new TextEncoder().encode(content);
  let rejected: string | undefined;
  return {
    from: "forwarder@example.test", to, headers: new Headers(), rawSize: rawSize ?? bytes.length,
    raw: new ReadableStream<Uint8Array>({ start(controller) { controller.enqueue(bytes); controller.close(); } }),
    canBeForwarded: false,
    setReject(reason: string) { rejected = reason; },
    async forward(): Promise<never> { throw new Error("Unexpected email send"); },
    async reply(): Promise<never> { throw new Error("Unexpected email send"); },
    get rejected() { return rejected; },
  };
}
async function acknowledgment(content = raw, duplicate = false) {
  return {
    receipt_id: Buffer.from(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(content))).toString("hex"),
    sequence: 1, received_at: "2026-10-07T10:00:00.000Z", duplicate,
  };
}
const logged = vi.fn();
beforeEach(() => { logged.mockClear(); vi.stubGlobal("console", { ...console, log: logged }); });
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); vi.useRealTimers(); });

it("posts exact raw bytes and authenticated envelope, awaiting a matching durable receipt", async () => {
  const receipt = await acknowledgment();
  let finish: ((value: Response) => void) | undefined;
  const send = vi.spyOn(globalThis, "fetch").mockImplementation(() => new Promise(resolve => { finish = resolve; }));
  let completed = false;
  const delivery = worker.email(mail(), env).then(() => { completed = true; });
  await vi.waitFor(() => expect(send).toHaveBeenCalledTimes(1));
  expect(completed).toBe(false);
  const [url, init] = send.mock.calls[0];
  expect(String(url)).toBe(env.INTAKE_URL);
  expect(init?.method).toBe("POST");
  expect(init?.redirect).toBe("manual");
  const headers = new Headers(init?.headers);
  expect(headers.get("authorization")).toBe(`Bearer ${env.INTAKE_TOKEN}`);
  expect(headers.get("CF-Access-Client-Id")).toBe(env.CF_ACCESS_CLIENT_ID);
  expect(headers.get("CF-Access-Client-Secret")).toBe(env.CF_ACCESS_CLIENT_SECRET);
  expect(headers.get("X-Alert-Envelope-From")).toBe("forwarder@example.test");
  expect(headers.get("X-Alert-Envelope-To")).toBe("alerts@example.com");
  expect(headers.get("X-Alert-Received-At")).toMatch(/^\d{4}-\d\d-\d\dT.*Z$/);
  expect(headers.get("content-type")).toBe("message/rfc822");
  expect(await new Response(init?.body).text()).toBe(raw);
  finish!(Response.json(receipt, { status: 201 }));
  await delivery;
  expect(completed).toBe(true);
  expect(logged).toHaveBeenLastCalledWith('{"event":"email_intake","outcome":"retained"}');
});

it("accepts an idempotent replay and preserved first receipt time", async () => {
  vi.spyOn(globalThis, "fetch").mockResolvedValue(Response.json(await acknowledgment(raw, true)));
  await worker.email(mail(), env);
  expect(logged).toHaveBeenLastCalledWith('{"event":"email_intake","outcome":"duplicate"}');
});

it("retains Gmail verification content without inventing sender authentication", async () => {
  const content = "From: forwarding-noreply@google.com\r\n\r\nSynthetic confirmation 123456";
  vi.spyOn(globalThis, "fetch").mockResolvedValue(Response.json(await acknowledgment(content), { status: 201 }));
  const message = mail(content);
  await worker.email(message, env);
  expect(message.rejected).toBeUndefined();
});

it("permanently rejects wrong recipient, invalid envelope, empty or excessive mail without POST", async () => {
  const send = vi.spyOn(globalThis, "fetch");
  const cases = [
    [mail(raw, "owner@example.com"), "Recipient not accepted"],
    [mail("", undefined), "Empty message"],
    [mail("x", undefined, 524289), "Message too large"],
    [mail("x".repeat(524289), undefined, 1), "Message too large"],
  ] as const;
  for (const [message, reason] of cases) { await worker.email(message, env); expect(message.rejected).toBe(reason); }
  const invalid = mail(); invalid.from = "injected\r\nHeader: value";
  await worker.email(invalid, env);
  expect(invalid.rejected).toBe("Invalid envelope");
  expect(send).not.toHaveBeenCalled();
});

it("bounds actual stream bytes and cancels oversize input without reading the remainder", async () => {
  let reads = 0; let cancelled = false;
  const message = mail();
  message.raw = new ReadableStream<Uint8Array>({
    pull(controller) { reads++; controller.enqueue(new Uint8Array(300000)); },
    cancel() { cancelled = true; },
  }, { highWaterMark: 0 });
  await worker.email(message, env);
  expect(message.rejected).toBe("Message too large");
  expect(reads).toBe(2); expect(cancelled).toBe(true);
});

it("fails closed on missing secrets, unsafe destinations and non-exact paths", async () => {
  const send = vi.spyOn(globalThis, "fetch");
  for (const key of ["INTAKE_TOKEN", "CF_ACCESS_CLIENT_ID", "CF_ACCESS_CLIENT_SECRET"] as const) {
    await expect(worker.email(mail(), { ...env, [key]: "" })).rejects.toThrow("Email intake configuration failed");
  }
  for (const url of ["http://intake.example.test/v1/email", "http://127.0.0.1/v1/email", "https://user:password@intake.example.test/v1/email", "https://intake.example.test/wrong", "https://intake.example.test/v1/email?secret=x", "https://intake.example.test/v1/email#x", "malformed"]) {
    await expect(worker.email(mail(), { ...env, INTAKE_URL: url })).rejects.toThrow("Email intake configuration failed");
  }
  expect(send).not.toHaveBeenCalled();
});

it("allows explicitly enabled loopback HTTP only for the local test harness", async () => {
  const send = vi.spyOn(globalThis, "fetch").mockImplementation(async () => Response.json(await acknowledgment()));
  for (const host of ["127.0.0.1", "localhost", "[::1]"]) {
    await worker.email(mail(), { ...env, INTAKE_URL: `http://${host}:8799/v1/email`, ALLOW_LOOPBACK_HTTP: "true" });
  }
  expect(send).toHaveBeenCalledTimes(3);
  await expect(worker.email(mail(), { ...env, INTAKE_URL: "http://example.test/v1/email", ALLOW_LOOPBACK_HTTP: "true" })).rejects.toThrow("configuration");
});

it.each([202, 204, 301, 302, 307, 308, 401, 403, 429, 500, 503, 507])("fails delivery on HTTP %i without following redirects", async status => {
  const send = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(null, {
    status, headers: { location: "https://untrusted.example.test/" },
  }));
  await expect(worker.email(mail(), env)).rejects.toThrow("Email intake delivery failed");
  expect(send).toHaveBeenCalledTimes(1);
  expect(send.mock.calls[0][1]?.redirect).toBe("manual");
});

it("rejects malformed, mismatched and structurally invalid acknowledgment receipts", async () => {
  const receipt = await acknowledgment();
  const send = vi.spyOn(globalThis, "fetch");
  const cases = [
    null, [], {}, { ...receipt, receipt_id: "f".repeat(64) },
    { ...receipt, sequence: 0 }, { ...receipt, sequence: 1.5 },
    { ...receipt, sequence: "1" }, { ...receipt, sequence: 9007199254740992 },
    { ...receipt, received_at: "2026-02-31T10:00:00Z" },
    { ...receipt, received_at: "yesterday" }, { ...receipt, duplicate: "false" },
  ];
  for (const value of cases) {
    send.mockResolvedValueOnce(Response.json(value));
    await expect(worker.email(mail(), env)).rejects.toThrow("Email intake acknowledgment failed");
  }
  for (const response of [new Response("not-json", { headers: { "content-type": "application/json" } }), new Response(JSON.stringify(receipt))]) {
    send.mockResolvedValueOnce(response);
    await expect(worker.email(mail(), env)).rejects.toThrow("acknowledgment");
  }
});

it("bounds acknowledgment bytes even when Content-Length is absent or false", async () => {
  let cancelled = false;
  const body = new ReadableStream<Uint8Array>({
    pull(controller) { controller.enqueue(new Uint8Array(4097)); }, cancel() { cancelled = true; },
  }, { highWaterMark: 0 });
  vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(body, { headers: { "content-type": "application/json", "content-length": "1" } }));
  await expect(worker.email(mail(), env)).rejects.toThrow("acknowledgment");
  expect(cancelled).toBe(true);
});

it("aborts a stalled POST after the bounded timeout", async () => {
  vi.useFakeTimers();
  let aborted = false;
  const send = vi.spyOn(globalThis, "fetch").mockImplementation((_url, init) => new Promise((_resolve, reject) => {
    init?.signal?.addEventListener("abort", () => { aborted = true; reject(new Error("private remote details")); });
  }));
  const outcome = expect(worker.email(mail(), env)).rejects.toThrow("Email intake delivery failed");
  await vi.waitFor(() => expect(send).toHaveBeenCalledOnce());
  await vi.advanceTimersByTimeAsync(10000);
  await outcome;
  expect(aborted).toBe(true);
});

it("also cancels a stalled acknowledgment body within the same timeout", async () => {
  vi.useFakeTimers();
  let cancelled = false;
  const body = new ReadableStream<Uint8Array>({ cancel() { cancelled = true; } });
  const send = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(body, { headers: { "content-type": "application/json" } }));
  const outcome = expect(worker.email(mail(), env)).rejects.toThrow("Email intake acknowledgment failed");
  await vi.waitFor(() => expect(send).toHaveBeenCalledOnce());
  await vi.advanceTimersByTimeAsync(10000);
  await outcome;
  expect(cancelled).toBe(true);
});

it("never logs or echoes raw content, addresses, URLs or secrets from remote failures", async () => {
  const sensitive = [raw, env.INTAKE_TOKEN, env.CF_ACCESS_CLIENT_ID, env.CF_ACCESS_CLIENT_SECRET, env.INTAKE_URL, mail().from];
  vi.spyOn(globalThis, "fetch").mockRejectedValue(new Error(sensitive.join(" ")));
  await expect(worker.email(mail(), env)).rejects.toThrow(/^Email intake delivery failed$/);
  const output = JSON.stringify(logged.mock.calls);
  for (const value of sensitive) expect(output).not.toContain(value);
  expect(logged).toHaveBeenCalledExactlyOnceWith('{"event":"email_intake","outcome":"delivery_failed"}');
});

it("exposes no public HTTP endpoint for injecting or reading email", async () => {
  for (const method of ["GET", "POST"]) {
    const response = await exports.default.fetch("https://worker.example.test/v1/email", { method });
    expect(response.status).toBe(404);
    expect(response.headers.get("cache-control")).toBe("no-store");
    expect(response.headers.get("access-control-allow-origin")).toBeNull();
  }
});
