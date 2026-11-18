import { env, exports } from 'cloudflare:workers';
import { applyD1Migrations, type D1Migration } from 'cloudflare:test';
import { beforeEach, expect, it } from 'vitest';
import worker, { type BufferedEnv } from '../src/buffered';
const bindings = env as unknown as BufferedEnv & { TEST_MIGRATIONS: D1Migration[] };
const token = 'a'.repeat(48);
const mime = 'From: jobalerts-noreply@linkedin.com\r\nSubject: fixture\r\n\r\noriginal';
function message(raw = mime) {
  return {to:'alerts@example.com', from:'forwarder@example.test', rawSize:raw.length,
    raw:new Blob([raw]).stream(), headers:new Headers(), setReject:()=>{throw Error('rejected');}} as unknown as ForwardableEmailMessage;
}
function request(path:string, body?:object, secret=token) {
  return new Request('https://buffer.example.test'+path,{method:body?'POST':'GET',
    headers:{Authorization:'Bearer '+secret,'content-type':'application/json'},body:body?JSON.stringify(body):undefined});
}
async function seedPending(count:number,bytes=1) {
 // Quota admission reads retained byte counts; synthetic rows avoid allocating a 64 MiB mail archive.
 await bindings.INBOX.prepare(`WITH RECURSIVE entries(n) AS (
  SELECT 1 UNION ALL SELECT n+1 FROM entries WHERE n < ?
 ) INSERT INTO mail(receipt_id,received_at,envelope_from,raw_base64,bytes)
 SELECT printf('%064x',n),'2026-11-05T00:00:00Z','seed@example.test','eA==',? FROM entries`)
  .bind(count,bytes).run();
}
beforeEach(async()=>{
 bindings.BUFFER_TOKEN=token;
 await bindings.INBOX.exec('DROP TABLE IF EXISTS mail; DROP TABLE IF EXISTS d1_migrations;');
 await applyD1Migrations(bindings.INBOX,bindings.TEST_MIGRATIONS);
});
it('loads the deployed entry point, D1 binding and pending-mail index from production configuration',async()=>{
 const response=await exports.default.fetch(request('/v1/messages'));
 expect(response.status).toBe(200);
 expect((await response.json() as {pending:number}).pending).toBe(0);
 const index=await bindings.INBOX.prepare("SELECT sql FROM sqlite_master WHERE type='index' AND name='pending_mail'").first<{sql:string}>();
 expect(index?.sql).toMatch(/ON mail\s*\(acknowledged_at,\s*sequence\)/i);
});
it('admits the exact Jack and Jill sender while refusing lookalikes and ambiguous senders',async()=>{
 await worker.email(message('From: Jack from Jack and Jill <jack@jackandjill.ai>\r\n\r\nfixture'),bindings);
 for(const from of ['jack@jackandjill.ai.evil.test','jill@jackandjill.ai','jack@jackandjill.ai, other@example.test',
  '"jack@jackandjill.ai" <other@example.test>','jack@jackandjill.ai\r\nFrom: other@example.test']) {
  await expect(worker.email(message('From: '+from+'\r\n\r\nfixture'),bindings)).rejects.toThrow('rejected');
 }
 expect((await (await worker.fetch(request('/v1/messages'),bindings)).json() as any).pending).toBe(1);
});
it('retains original once and repeats it until the VPS acknowledges durable storage',async()=>{
 await worker.email(message(),bindings); await worker.email(message(),bindings);
 const first=await (await worker.fetch(request('/v1/messages'),bindings)).json() as any;
 expect(first.pending).toBe(1); expect(first.messages).toHaveLength(1);
 expect(atob(first.messages[0].raw_base64)).toBe(mime);
 const again=await (await worker.fetch(request('/v1/messages'),bindings)).json() as any;
 expect(again.messages).toEqual(first.messages);
 await worker.fetch(request('/v1/ack',{receipts:[first.messages[0].receipt_id]}),bindings);
 await worker.email(message(),bindings);
 const done=await (await worker.fetch(request('/v1/messages'),bindings)).json() as any;
 expect(done.pending).toBe(0); expect(done.total_received).toBe(1); expect(done.last_received_at).toBeTruthy();
});
it('does not expose mail or permit acknowledgment without the scoped token',async()=>{
 await worker.email(message(),bindings);
 expect((await worker.fetch(request('/v1/messages',undefined,'wrong'),bindings)).status).toBe(401);
 expect((await worker.fetch(request('/v1/ack',{receipts:[]},'wrong'),bindings)).status).toBe(401);
 bindings.BUFFER_TOKEN='';
 expect((await worker.fetch(request('/v1/messages'),bindings)).status).toBe(503);
});
it('refuses malformed acknowledgments without dropping pending mail',async()=>{
 await worker.email(message(),bindings);
 expect((await worker.fetch(request('/v1/ack',{receipts:['bad']}),bindings)).status).toBe(400);
 expect((await (await worker.fetch(request('/v1/messages'),bindings)).json() as any).pending).toBe(1);
});
it('fails delivery if durable storage is unavailable',async()=>{
 await bindings.INBOX.exec('DROP TABLE mail');
 await expect(worker.email(message(),bindings)).rejects.toThrow();
});

it('serializes concurrent admission at 1,000 pending messages and releases one place on acknowledgment',async()=>{
 await seedPending(999);
 const contents=[mime+' one',mime+' two'];
 const attempts=await Promise.allSettled(contents.map(raw=>worker.email(message(raw),bindings)));
 expect(attempts.filter(attempt=>attempt.status==='fulfilled')).toHaveLength(1);
 const refused=attempts.filter(attempt=>attempt.status==='rejected');
 expect(refused).toHaveLength(1);
 expect(refused[0].reason.message).toBe('Email buffer capacity exceeded');
 const stored=await bindings.INBOX.prepare("SELECT receipt_id FROM mail WHERE envelope_from='forwarder@example.test'").first<{receipt_id:string}>();
 expect(stored).not.toBeNull();
 let stats=await (await worker.fetch(request('/v1/messages'),bindings)).json() as any;
 expect(stats.pending).toBe(1000);
 expect(stats.total_received).toBe(1000);
 // A replay is successful even at capacity, and cannot occupy another place.
 const winner=attempts.findIndex(attempt=>attempt.status==='fulfilled');
 await worker.email(message(contents[winner]),bindings);
 await worker.fetch(request('/v1/ack',{receipts:[stored!.receipt_id]}),bindings);
 await worker.email(message(contents[winner]),bindings);
 await worker.email(message(contents[1-winner]),bindings);
 stats=await (await worker.fetch(request('/v1/messages'),bindings)).json() as any;
 expect(stats.pending).toBe(1000);
 expect(stats.total_received).toBe(1001);
 const acknowledged=await bindings.INBOX.prepare('SELECT raw_base64,acknowledged_at FROM mail WHERE receipt_id=?')
  .bind(stored!.receipt_id).first<{raw_base64:string|null,acknowledged_at:string}>();
 expect(acknowledged?.raw_base64).toBeNull();
 expect(acknowledged?.acknowledged_at).toBeTruthy();
});

it('accepts exactly 64 MiB of pending bytes and refuses the next distinct message without losing mail',async()=>{
 const incomingBytes=new TextEncoder().encode(mime).byteLength;
 await seedPending(127,524288);
 await bindings.INBOX.prepare(`INSERT INTO mail(receipt_id,received_at,envelope_from,raw_base64,bytes)
  VALUES(?,'2026-11-05T00:00:00Z','seed@example.test','eA==',?)`)
  .bind('f'.repeat(64),524288-incomingBytes).run();
 await worker.email(message(),bindings);
 await expect(worker.email(message(mime+' extra'),bindings)).rejects.toThrow('Email buffer capacity exceeded');
 await worker.email(message(),bindings);
 const retained=await bindings.INBOX.prepare('SELECT COUNT(*) count,SUM(bytes) bytes FROM mail WHERE acknowledged_at IS NULL')
  .first<{count:number,bytes:number}>();
 expect(retained).toEqual({count:129,bytes:67108864});
});

it('accepts 512 KiB exactly but rejects reported or streamed oversize input before storage',async()=>{
 const exact=mime+'x'.repeat(524288-new TextEncoder().encode(mime).byteLength);
 await worker.email(message(exact),bindings);
 let reads=0,cancelled=false;
 const stream=()=>new ReadableStream<Uint8Array>({
  pull(controller){reads++;controller.enqueue(new Uint8Array(300000));},
  cancel(){cancelled=true;},
 },{highWaterMark:0});
 const reported={...message(),rawSize:524289,raw:stream()};
 await expect(worker.email(reported,bindings)).rejects.toThrow('rejected');
 expect(reads).toBe(0);
 const actual={...message(),rawSize:1,raw:stream()};
 await expect(worker.email(actual,bindings)).rejects.toThrow('rejected');
 expect(reads).toBe(2);
 expect(cancelled).toBe(true);
 const retained=await bindings.INBOX.prepare('SELECT COUNT(*) count,SUM(bytes) bytes FROM mail').first();
 expect(retained).toEqual({count:1,bytes:524288});
});

it('rejects unrelated or ambiguous MIME senders before they consume storage',async()=>{
 for (const header of [
  'From: unrelated@example.test',
  'From: jobalerts-noreply@linkedin.com.attacker.test',
  'From: jobalerts-noreply@linkedin.com, other@example.test',
  'From: jobalerts-noreply@linkedin.com\r\nFrom: other@example.test',
  'From: other@example.test\r\nFrom: jobalerts-noreply@linkedin.com',
  'From: "jobalerts-noreply@linkedin.com" <other@example.test>',
  'From: other@example.test ( <jobalerts-noreply@linkedin.com>',
  'From: other@example.test <jobalerts-noreply@linkedin.com>',
  'From: LinkedIn: <jobalerts-noreply@linkedin.com>',
  'From: jobalerts-noreply@linkedin.com\rSubject: malformed',
  'From: jobalerts-noreply@linkedin.com\r\nX-Padding: '+'x'.repeat(65536),
  'Subject: no sender',
 ]) {
  await expect(worker.email(message(header+'\r\n\r\nfixture'),bindings)).rejects.toThrow('rejected');
 }
 expect((await (await worker.fetch(request('/v1/messages'),bindings)).json() as any).total_received).toBe(0);
});

it('accepts forwarded alert display names, folded headers and Google confirmation mail',async()=>{
 for (const from of ['LinkedIn Job Alerts <jobalerts-noreply@linkedin.com>',
  '"LinkedIn, Job Alerts"\r\n <jobalerts-noreply@linkedin.com>',
  '=?UTF-8?Q?LinkedIn_Alerts?= <jobalerts-noreply@linkedin.com>',
  'Gmail Team <forwarding-noreply@google.com>']) {
  await worker.email(message('From: '+from+'\r\n\r\nfixture'),bindings);
 }
 expect((await (await worker.fetch(request('/v1/messages'),bindings)).json() as any).pending).toBe(4);
});

it('continues after a retained bad item without acknowledging or hiding it',async()=>{
 for(let i=0;i<12;i++) await worker.email(message(mime+i),bindings);
 const first=await (await worker.fetch(request('/v1/messages'),bindings)).json() as any;
 expect(first.messages.map((m:any)=>m.sequence)).toEqual([1,2,3,4,5,6,7,8,9,10]);
 const next=await (await worker.fetch(request('/v1/messages?after=10'),bindings)).json() as any;
 expect(next.messages.map((m:any)=>m.sequence)).toEqual([11,12]);
 expect(next.pending).toBe(12);
 for(const query of ['after=-1','after=1&after=2','after=oops','after=1.5','after=9007199254740992','ignored=1']) {
  expect((await worker.fetch(request('/v1/messages?'+query),bindings)).status).toBe(400);
 }
});

it('rejects null, primitive, duplicate, invalid UTF-8 and oversized acknowledgment bodies as client errors',async()=>{
 await worker.email(message(),bindings);
 const page=await (await worker.fetch(request('/v1/messages'),bindings)).json() as any;
 const id=page.messages[0].receipt_id;
 for(const body of ['null','[]','true','"text"',JSON.stringify({receipts:[id,id]}),
  new Uint8Array([...new TextEncoder().encode('{"receipts":["'+id+'"],"padding":"'),0xff,34,125]),
  JSON.stringify({receipts:[id],padding:'é'.repeat(1100)})]) {
  const response=await worker.fetch(new Request('https://buffer.example.test/v1/ack',{
   method:'POST',headers:{Authorization:'Bearer '+token},body}),bindings);
  expect(response.status).toBe(400);
 }
 expect((await (await worker.fetch(request('/v1/messages'),bindings)).json() as any).pending).toBe(1);
});

it('reports only actual stored identities as acknowledged and supports replay after a lost response',async()=>{
 const missing=await (await worker.fetch(request('/v1/ack',{receipts:['0'.repeat(64)]}),bindings)).json() as any;
 expect(missing.acknowledged).toBe(0);
 await worker.email(message(),bindings);
 const page=await (await worker.fetch(request('/v1/messages'),bindings)).json() as any;
 const id=page.messages[0].receipt_id;
 for(let i=0;i<2;i++) {
  expect((await (await worker.fetch(request('/v1/ack',{receipts:[id]}),bindings)).json() as any).acknowledged).toBe(1);
 }
});
