import { Buffer } from 'node:buffer';
import emailSources from '../../../config/email-sources.json';

export interface BufferedEnv { INBOX: D1Database; BUFFER_TOKEN: string; }
const RECIPIENT = 'alerts@example.com';
const MAX_BYTES = 512 * 1024;
const MAX_PENDING_BYTES = 64 * 1024 * 1024;
const MAX_PENDING = 1000;
const ADMITTED_SENDERS = new Set([...Object.values(emailSources).map(source => source.sender), 'forwarding-noreply@google.com']);
const json = (value:unknown, status=200) => Response.json(value,{status,headers:{'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'}});

function admittedMail(raw:Uint8Array):boolean {
  // Admission is narrower than authentication: Cloudflare enforces the sender's DMARC policy.
  // Read the original headers; never trust caller-supplied Authentication-Results or a display name.
  const boundary = /\r?\n\r?\n/.exec(new TextDecoder('latin1').decode(raw.subarray(0,65540)));
  if (!boundary || boundary.index === 0 || boundary.index > 65536) return false;
  const header = new TextDecoder().decode(raw.subarray(0,boundary.index));
  if (/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]|\r(?!\n)/.test(header)) return false;
  const fields:string[]=[];
  for (const line of header.split(/\r?\n/)) {
    if (/^[ \t]/.test(line)) {
      if (!fields.length) return false;
      fields[fields.length-1] += ' '+line.trim();
    } else {
      if (!/^[!-9;-~]+:/.test(line)) return false;
      fields.push(line);
    }
  }
  const from = fields.filter(line=>/^from:/i.test(line));
  if (from.length !== 1) return false;
  const value = from[0].slice(5).trim().toLowerCase();
  if (ADMITTED_SENDERS.has(value)) return true;
  // An unquoted display name cannot contain mailbox/group/comment syntax.
  const named = /^(?:"(?:[^"\\\r\n]|\\[^\r\n])*"[ \t]*|[^<>()\[\]@,;:"\\\r\n]*)<([^<>]+)>$/.exec(value);
  return !!named && ADMITTED_SENDERS.has(named[1]);
}
async function authorized(request:Request, token:string):Promise<boolean> {
  // Fixed-size digests avoid leaking token prefixes through string comparison.
  const encode = (value:string) => crypto.subtle.digest('SHA-256',new TextEncoder().encode(value));
  const [a,b] = await Promise.all([encode(request.headers.get('authorization') || ''),encode('Bearer '+token)]);
  return crypto.subtle.timingSafeEqual(a,b);
}
export default {
 async email(message:ForwardableEmailMessage, env:BufferedEnv):Promise<void> {
  if(message.to!==RECIPIENT || message.from.length>320 || /[\r\n\0]/.test(message.from)) {
   message.setReject('Recipient or envelope not accepted'); return;
  }
  if(message.rawSize>MAX_BYTES) { message.setReject('Message too large'); return; }
  const reader=message.raw.getReader(); const chunks:Uint8Array[]=[]; let size=0;
  try { while(true) { const {value,done}=await reader.read(); if(done) break;
   size+=value.byteLength; if(size>MAX_BYTES) {await reader.cancel(); message.setReject('Message too large');return;} chunks.push(value);
  }} finally {reader.releaseLock();}
  if(!size) {message.setReject('Empty message');return;}
  const raw=Buffer.concat(chunks);
  if(!admittedMail(raw)) {message.setReject('Only configured job alerts and forwarding confirmation mail are accepted');return;}
  const hash=Buffer.from(await crypto.subtle.digest('SHA-256',raw)).toString('hex');
  const now=new Date().toISOString();
  // One conditional insert serializes quota accounting with storage. Keep acknowledged identities:
  // replay after a lost ack must not create another pending message. Raw bytes are released only on ack.
  await env.INBOX.prepare(`INSERT OR IGNORE INTO mail(receipt_id,received_at,envelope_from,raw_base64,bytes)
   SELECT ?,?,?,?,? WHERE (SELECT COUNT(*) FROM mail WHERE acknowledged_at IS NULL) < ?
   AND (SELECT COALESCE(SUM(bytes),0) FROM mail WHERE acknowledged_at IS NULL)+? <= ?`)
   .bind(hash,now,message.from,raw.toString('base64'),size,MAX_PENDING,size,MAX_PENDING_BYTES).run();
  const saved=await env.INBOX.prepare('SELECT receipt_id FROM mail WHERE receipt_id=?').bind(hash).first();
  if(!saved) throw new Error('Email buffer capacity exceeded');
 },
 async fetch(request:Request, env:BufferedEnv):Promise<Response> {
  if(!env.BUFFER_TOKEN || env.BUFFER_TOKEN.length<32) return json({error:'unavailable'},503);
  if(!(await authorized(request,env.BUFFER_TOKEN))) return json({error:'unauthorized'},401);
  const url=new URL(request.url),path=url.pathname;
  try {
   if(request.method==='GET' && path==='/v1/messages') {
    const values=url.searchParams.getAll('after'),value=values[0]??'0';
    if([...url.searchParams.keys()].some(key=>key!=='after') || values.length>1 || !/^(0|[1-9]\d*)$/.test(value) || !Number.isSafeInteger(Number(value))) return json({error:'invalid_cursor'},400);
    // Use a single D1 batch for a consistent message/count snapshot. Never mark read on GET.
    const [messages,stats]=await env.INBOX.batch([
     env.INBOX.prepare('SELECT sequence,receipt_id,received_at,envelope_from,raw_base64 FROM mail WHERE acknowledged_at IS NULL AND sequence>? ORDER BY sequence LIMIT 10').bind(Number(value)),
     env.INBOX.prepare('SELECT COUNT(*) total_received, MAX(received_at) last_received_at, COALESCE(SUM(CASE WHEN acknowledged_at IS NULL THEN 1 ELSE 0 END),0) pending FROM mail')]);
    return json({messages:messages.results,...(stats.results[0] as Record<string,unknown>),recipient:RECIPIENT});
   }
   if(request.method==='POST' && path==='/v1/ack') {
    if(Number(request.headers.get('content-length')||0)>2048) return json({error:'invalid_ack'},400);
    // Bound a streamed request too; Content-Length alone is not a limit.
    const reader=request.body?.getReader(); if(!reader) return json({error:'invalid_ack'},400);
    let text='',size=0; const decoder=new TextDecoder('utf-8',{fatal:true,ignoreBOM:false});
    try { while(true){const {done,value}=await reader.read();if(done) break;size+=value.byteLength;if(size>2048){await reader.cancel();return json({error:'invalid_ack'},400);}text+=decoder.decode(value,{stream:true});}text+=decoder.decode(); }
    catch {await reader.cancel().catch(()=>{});return json({error:'invalid_ack'},400);}
    finally {reader.releaseLock();}
    let body:unknown; try {body=JSON.parse(text);} catch {return json({error:'invalid_ack'},400);}
    if(!body || typeof body!=='object' || Array.isArray(body) || !('receipts' in body)) return json({error:'invalid_ack'},400);
    const ids=body.receipts;
    if(!Array.isArray(ids) || !ids.length || ids.length>10 || ids.some((v:unknown)=>typeof v!=='string'||!/^[a-f0-9]{64}$/.test(v)) || new Set(ids).size!==ids.length) return json({error:'invalid_ack'},400);
    const results=await env.INBOX.batch(ids.map((id:string)=>env.INBOX.prepare('UPDATE mail SET raw_base64=NULL,acknowledged_at=COALESCE(acknowledged_at,?) WHERE receipt_id=?').bind(new Date().toISOString(),id)));
    return json({acknowledged:results.reduce((sum,result)=>sum+result.meta.changes,0)});
   }
   return json({error:'not_found'},404);
  } catch {return json({error:'storage_unavailable'},503);}
 }
};
