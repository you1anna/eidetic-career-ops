// Rebuild all current architecture diagrams. Uses the pinned browser renderer, no live data.
// Run: node tools/render_system_map.mjs (npm ci --prefix tests/ui; see ops/LOCAL.md).
import { createRequire } from 'node:module';
import { writeFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { launchBrowser } from './browser_launch.mjs';
const require = createRequire(new URL('../tests/ui/package.json', import.meta.url));
const { chromium } = require('playwright-core');
const colors={ink:'#173c3b',muted:'#51686b',flow:'#257b7a',research:'#7660a4',admin:'#8b6d37',line:'#c9d9d8',paper:'#f5f8f8'};
const esc=s=>String(s).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;');
function diagram(name,title,subtitle,width,height){
 const p=[],labels=[];
 const text=(x,y,s,size=20,color=colors.ink,weight=400)=>p.push(`<text x="${x}" y="${y}" font-size="${size}" fill="${color}" font-weight="${weight}">${esc(s)}</text>`);
 function wrap(s,max){const out=[];let line='';for(const w of s.split(' ')){if((line+' '+w).trim().length>max&&line){out.push(line);line=w;}else line=(line+' '+w).trim();}if(line)out.push(line);return out;}
 function box(x,y,w,h,title,lines,kind='normal'){
  const styles={normal:['#fff',colors.line],data:['#e0f1eb','#8db7a5'],research:['#f1edf8','#b7a7ce'],warning:['#fff4db','#d3ae64']};
  const [fill,stroke]=styles[kind];
  p.push(`<g data-box="${esc(title)}"><rect x="${x}" y="${y}" width="${w}" height="${h}" rx="14" fill="${fill}" stroke="${stroke}" stroke-width="2"/>`);
  text(x+20,y+33,title,22,colors.ink,700);
  const body=wrap(lines.join(' '),Math.floor((w-40)/10.5));
  body.forEach((s,i)=>text(x+20,y+64+i*24,s,18,colors.muted));
  if(64+(body.length-1)*24+10>h)throw Error(`Box too short: ${title} (${body.length} lines)`);
  p.push('</g>');
 }
 function zone(x,y,w,h,title,fill='#edf3f3'){
  p.push(`<rect x="${x}" y="${y}" width="${w}" height="${h}" rx="20" fill="${fill}" stroke="${colors.line}" stroke-width="2"/>`);
  text(x+22,y+35,title,22,colors.ink,700);
 }
 function arrow(points,kind='flow',label=null,lx=0,ly=0){
  const c=colors[kind];const d=points.map(([x,y],i)=>`${i?'L':'M'}${x},${y}`).join(' ');
  p.push(`<path d="${d}" fill="none" stroke="${c}" stroke-width="3" ${kind==='flow'?'':'stroke-dasharray="8 6"'} marker-end="url(#${kind})"/>`);
  if(label){const w=label.length*9.2+18;labels.push(`<rect x="${lx-8}" y="${ly-20}" width="${w}" height="29" rx="5" fill="${colors.paper}"/><text x="${lx}" y="${ly}" font-size="18" fill="${c}" font-weight="600">${esc(label)}</text>`);}
 }
 p.push(`<rect width="${width}" height="${height}" fill="${colors.paper}"/>`);
 text(40,58,title,38,colors.ink,700);text(40,96,subtitle,20,colors.muted);
 text(40,133,'Solid teal: server data flow  •  Dashed purple: model-assisted research  •  Dashed brown: human action / administration',18,colors.muted);
 return {box,zone,arrow,text,async render(browser){
  const defs=['flow','research','admin'].map(k=>`<marker id="${k}" markerWidth="9" markerHeight="9" refX="8" refY="4.5" orient="auto"><path d="M0,0 L9,4.5 L0,9 Z" fill="${colors[k]}"/></marker>`).join('');
  const svg=`<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}" role="img" aria-label="${esc(title)}"><title>${esc(title)}</title><desc>${esc(subtitle)}</desc><defs>${defs}</defs><g font-family="Arial, Helvetica, sans-serif">${p.join('\n')}${labels.join('\n')}</g></svg>`;
  await writeFile(new URL(`../docs/${name}.svg`,import.meta.url),svg);
  const page=await browser.newPage({viewport:{width,height},deviceScaleFactor:1.5});
  try{
   await page.setContent(`<html><body style="margin:0">${svg}</body></html>`);
   const errors=await page.evaluate(()=>[...document.querySelectorAll('[data-box]')].flatMap(g=>{const r=g.querySelector('rect').getBBox();return [...g.querySelectorAll('text')].filter(t=>{const b=t.getBBox();return b.x<r.x||b.x+b.width>r.x+r.width-8||b.y+b.height>r.y+r.height-8;}).map(t=>g.dataset.box+': '+t.textContent);}));
   if(errors.length)throw Error(errors.join('\n'));
   await page.screenshot({path:fileURLToPath(new URL(`../docs/${name}.png`,import.meta.url))});
  }finally{await page.close();}
  console.log(`Generated docs/${name}.svg and .png; box text bounds passed`);
 }};
}
const full=diagram('system-map','work-ops | the complete lifecycle','26 October 2026 • Opportunity-led implementation, not deployment evidence • Automatic data refresh; attended software releases',1920,2730);
{
const {zone,box,arrow,text}=full;
const lane=(y,label,items,kind='normal')=>{
 zone(40,y,1840,295,label,kind==='research'?'#f1edf8':'#edf3f3');
 items.forEach(([title,lines],i)=>{box(70+i*610,y+65,560,195,title,lines,kind);if(i<2 && !(y===1830 && i===1))arrow([[630+i*610,y+160],[680+i*610,y+160]],kind==='research'?'research':(y===840||y>=1500)?'admin':'flow');});
};
lane(180,'1  DISCOVER • automatic on the VPS; no Codex needed',[
 ['Configured public sources',['Employer boards, Reed/Adzuna, HN, tenders, awards, funding and company records. Daily ~05:30 UTC. Only configured/approved sources.']],
 ['Scan → canonical import',['Match source IDs and compatible records. Retain evidence and observations. Missing facts stay unknown; preserve human edits.']],
 ['Live SQLite database',['One authoritative store on the VPS. Jobs, people, organisations, source evidence, drafts, decisions, receipts and outcomes.']]
]);
lane(510,'2  RECEIVE ALERTS • a separate automatic path into the same records',[
 ['Gmail → Cloudflare buffer',['Only the exact LinkedIn job-alert sender. Gmail keeps originals. Private D1 retains mail until the VPS confirms durable receipt.']],
 ['VPS email worker • every 15 min',['Pull → private spool → acknowledge → parse → canonical import. Hashes deduplicate retries. Retained mail survives parser failure.']],
 ['Candidates + delivery receipts',['New or existing jobs receive observations. Empty polling proves execution only. Email arrival never confirms a vacancy is open.']]
]);
lane(840,'3  ADD AN INCOMING ROLE • attended input; no recruiter-mail automation',[
 ['Recruiter, referral or existing process',['Add incoming role in Jobs or This week. Record the actual stage and dates; include private source evidence.']],
 ['Preview → canonical save',['Match an existing role before saving. Keep employer, agency, recruiters and documents. No invented application or sent message.']],
 ['Active process → focused research',['Preserve discovery and relationship history. Private documents supply context; they never verify a public vacancy or contact.']]
]);
lane(1170,'4  RESEARCH • 09:00 / 19:00 Europe/London • limited maintained scope',[
 ['Select + read public evidence',['Active and shortlisted work plus the top ten discovery opportunities. Rotate oldest attempts; active work first on ties. Blocked pages stay gaps.']],
 ['Isolated Codex CLI → validation',['ChatGPT account; selected context only. Search finds URLs; actual fetched quotes support claims. Scheduled session: no shell, record writes or code changes.']],
 ['Save supported results',['Dated vacancy/route checks, demand evidence, terms and unknowns. Reuse current evidence. No generated messages or new draft approvals.']]
],'research');
lane(1500,'5  ACT • ranked work and optional factual tracking; compose and send in Gmail',[
 ['Private site → weekly action view',['Access PIN → Tunnel → loopback site. Active work leads, then ten ranked opportunities, with more available. Jobs owns the full lifecycle.']],
 ['The owner sends outside the site',['Use work assessments to decide. Compose and send in Gmail or an application site. People keeps relationship context and contact history.']],
 ['Record outcome → next action',['Optionally record factual contact, reply and next step. Shared progress controls preserve retries and Undo. Saved wording stays read-only in history.']]
]);
lane(1830,'6  MONITOR + RECOVER • deterministic checks; no autonomous repair',[
 ['Caretaker • every 15 min',['Checks services, timers, receipts, source health and backup evidence. Zero model calls. Healthy, partial, waiting, unknown or failed.']],
 ['Visible status → attended response',['Failure causes appear on cards. Completed research can recover with source gaps; repeated crashes stay separate. /caretaker starts attended diagnosis.']],
 ['Backups + recovery evidence',['02:15 UTC: encrypted snapshots on the VPS. 07:30 Mac-local: site DB copy + restore drill. Mail/raw research stay on VPS; Gmail keeps mail.']]
]);
lane(2160,'7  CHANGE SOFTWARE • human-directed development and an explicitly authorised release',[
 ['Problem → agent work → tests',['The owner directs work; an attended agent inspects evidence and changes code/docs. Synthetic fixtures and strict browser/contract tests.']],
 ['Push main → authorised deploy',['Private GitHub holds code, not live records. Push does not deploy. /deploy needs an authorised operator route; the VPS record gateway cannot deploy.']],
 ['Verify hosted site → review value',['Check serving revision + hosted health. The owner reviews usefulness; findings inform the next change. VPS GitHub access is read-only.']]
]);
text(60,2530,'DATA LOOP: source → validated import → live database → page refresh. No deployment is needed for record updates.',23,colors.ink,700);
text(60,2575,'CODE LOOP: evidence → attended changes → tests → push → authorised deploy → hosted checks → human review.',23,colors.ink,700);
text(60,2620,'NOT IMPLEMENTED: scheduled Codex diagnosis, weekly software-improvement review, automatic code repair or deployment.',22,colors.admin,700);
text(60,2670,'Research caps: 6 calls/pass, 12/day, 84/week and 30 targets/pass. One shared selection; no page-triggered research.',20,colors.muted);
}
const email=diagram('email-processing','LinkedIn alerts | delivery, processing and proof','What happens to one email • The receiving and processing path runs without the Mac',1800,1280);
{
const {box,arrow,zone,text}=email;
zone(40,180,1720,280,'1  DELIVERY • Gmail and Cloudflare');
box(70,245,480,160,'Gmail selective forwarding',['Sender: jobalerts-noreply@linkedin.com','Destination: alerts@example.com','Keep Inbox/unread state + Work-ops label.']);
box(650,245,480,160,'Cloudflare Email Worker',['Email Routing invokes the Worker.','Raw MIME + envelope + hash go into D1.','A committed write precedes acceptance.']);
box(1230,245,480,160,'Durable D1 buffer',['Retains mail during VPS downtime.','Authenticated batches, up to 10 messages.','No public mailbox or raw-mail browser.'],'data');
arrow([[550,325],[650,325]]);arrow([[1130,325],[1230,325]]);
zone(40,505,1720,300,'2  SERVER PROCESSING • every 15 minutes, up to 30 seconds jitter');
box(1230,575,480,175,'Pull → store → acknowledge',['VPS verifies the MIME hash.','Commits original to private SQLite spool.','Only then ACKs: D1 releases its raw copy.','Lost ACK replays safely; hash deduplicates.'],'data');
box(650,575,480,175,'Existing parser + role rules',['Reads retained mail, up to 200 per pass.','Extracts adverts and canonical job URLs.','Keeps exclusions and quarantine evidence.','Unknown facts remain unknown.']);
box(70,575,480,175,'One canonical importer',['Matches existing records and organisations.','Preserves edits, notes and workflow state.','Retains original source dates and evidence.','Email alone never refreshes source checks.']);
arrow([[1470,405],[1470,575]],'flow','VPS polls HTTPS',1485,487);
arrow([[1230,660],[1130,660]]);arrow([[650,660],[550,660]]);
zone(40,850,1720,250,'3  WHAT OWNER SEES • Jobs → LinkedIn email processing');
box(70,920,480,150,'Run receipt + job candidates',['Last attempt and successful check.','Import outcomes and source evidence.','Useful candidates appear in Jobs.'],'data');
box(650,920,480,150,'Honest status',['Empty polling proves execution only.','Failure / partial / stalled remain visible.','Overdue after 45 minutes without a run.'],'warning');
box(1230,920,480,150,'Inspect delivery details',['Last arrival and last alert processed.','Parsed/matching counts and waiting mail.','Refresh the page for the newest receipt.']);
arrow([[310,750],[310,825],[55,825],[55,985],[70,985]]);arrow([[550,985],[650,985]]);arrow([[1130,985],[1230,985]]);
text(40,1150,'Recovery: collection failures retry; retained mail can still process. Parser quarantine needs attended investigation.',22,colors.ink,700);
text(40,1190,'Limits: 512 KiB/message; VPS: 1,000 retained messages; D1: 1,000 pending messages. Each has a 64 MiB raw-mail cap.',20,colors.muted);
text(40,1225,'Delivery recovery: trace a fresh alert through scheduled processing and matching Jobs records; an empty poll is not proof.',20,colors.muted);
text(40,1255,'Mail arrival never refreshes source checks. Separate focused research adds dated evidence; the release ledger records activation and observed recovery.',19,colors.muted);
}
const research=diagram('research-and-freshness','Research | evidence, decisions and limits','26 October 2026 • Evidence-only research • Successful execution is not proof of useful output',1800,1740);
{
const {box,arrow,zone,text}=research;
zone(40,180,1720,280,'1  SELECT • twice daily; no Mac needed','#f1edf8');
box(70,245,480,190,'Maintained set',['Active/shortlisted work and the top ten discovery opportunities. One shared server policy. Excluded, skipped, archived and closed targets stay out.'],'research');
box(650,245,480,190,'Recorded context + public reads',['Work remit, fit, terms and known gaps. Exact public pages, up to four discovery alternatives. Saved private sources are context only; no private mail fetch.'],'research');
box(1230,245,480,190,'Durable allowance admission',['6 calls/pass; 12/day, 84/week; 30 targets/pass. Failed/unknown calls count. A cap leaves targets waiting. Existing schedules remain.'],'warning');
arrow([[550,330],[650,330]],'research');arrow([[1130,330],[1230,330]],'research');
zone(40,500,1720,320,'2  ASSESS EVIDENCE • reuse current findings; research decisive gaps','#f1edf8');
box(70,565,480,205,'Current context: reuse or refresh',['Reuse current checks and unchanged context. Rotate eligible work by oldest attempt. Never generate outgoing wording or infer facts from old generated text.'],'research');
box(650,565,480,205,'Evidence brief: support or gap',['Known public pages + bounded URL discovery. Extract attributed quotations and route claims. Unsupported or ambiguous claims are withheld. No evidence means an explicit gap.'],'research');
box(1230,565,480,205,'Trusted worker checks the result',['Codex cannot write records. Match claims to actual fetched text; validate identity and policy. Recheck revisions and links in a short transaction; preserve human edits and history.'],'data');
arrow([[310,435],[310,480],[55,480],[55,650],[70,650]],'research');arrow([[890,435],[1060,480],[1060,565]],'research');
arrow([[550,680],[600,680],[600,795],[1200,795],[1200,680],[1230,680]],'research');
arrow([[1130,640],[1230,640]],'research');
zone(40,860,1720,285,'3  SAVE → DISPLAY → HUMAN ACTION');
box(70,925,480,200,'Canonical evidence + replay',['Immutable batches precede import. Replay deduplicates and revalidates without another call; original source-read times stay intact. Save sources, read dates, checks and gaps. Old preparation batches cannot create drafts.'],'data');
box(650,925,480,200,'Reload the site to inspect',['Assessment: remit, fit, demand, terms and unknowns. Jobs: lifecycle and research usage. This week: active work and ranked discovery. People: relationships.']);
box(1230,925,480,200,'The owner decides and records',['Compose and send in Gmail or apply externally. Optionally track factual contact, reply and next step. Historical wording remains readable. No automatic personal follow-ups.']);
arrow([[1470,770],[1470,840],[55,840],[55,1015],[70,1015]],'research');
arrow([[550,1015],[650,1015]]);arrow([[1130,1015],[1230,1015]]);
zone(40,1185,1720,300,'4  FRESHNESS IS A SEPARATE QUESTION FROM CONTEXT');
box(70,1250,480,190,'Confirmed • less than 24 hours',['Exact vacancy or personal/employer route supported by a source read. A profile is not proof of message delivery, hiring authority or willingness to help.'],'data');
box(650,1250,480,190,'Time passes → needs recheck',['A record edit, new alert or cached answer cannot refresh the clock. Cached interpretations need freshly fetched matching text. Checks under 8 hours can be skipped.'],'warning');
box(1230,1250,480,190,'Blocked, conflicting or unknown',['No bypass of login/robots barriers. A later failed public check can replace earlier public confirmation. The owner-confirmed/prior-correspondence evidence stays separate.'],'warning');
arrow([[550,1340],[650,1340]],'research');
text(40,1540,'Possible useful output: an open advert + essential requirement; an exact public profile; a sourced remit; a decisive unknown.',22,colors.ink,700);
text(40,1585,'Possible honest output: no new facts, inaccessible pages or waiting targets. A completed run can have zero useful yield.',21,colors.admin,700);
text(40,1630,'Measure new action-changing evidence, unsuitable jobs ruled out and decision time saved—not calls made or green service status.',20,colors.muted);
text(40,1675,'This worker maintains selected career records. It does not improve application code or deploy the site.',22,colors.ink,700);
}
const lifecycle=diagram('software-lifecycle','Software improvement | the attended loop','22 October 2026 • This loop needs the owner and an operator • It is not a scheduled self-improvement service',1800,1110);
{
const {box,arrow,zone,text}=lifecycle;
zone(40,180,1720,300,'1  NOTICE → CHOOSE → BUILD');
box(70,245,480,205,'Observe a problem or opportunity',['Experience, receipts, research gaps and failure causes supply evidence. /caretaker brings findings into an attended chat; it is not automatic diagnosis.']);
box(650,245,480,205,'The owner directs attended work',['An agent investigates and proposes/implements an improvement within the request. Product priorities and missing release authority are resolved with the owner.']);
box(1230,245,480,205,'Implement and validate locally',['Code + documentation; synthetic fixtures. Test consequential behavior, HTTP contracts, desktop Chromium and iPhone WebKit. No live-data test writes.']);
arrow([[550,335],[650,335]],'admin');arrow([[1130,335],[1230,335]],'admin');
zone(40,520,1720,300,'2  RELEASE → VERIFY → LEARN');
box(1230,585,480,205,'Push main; authorise release',['GitHub stores code and documentation. A push changes no live behavior. /deploy requests the shared release workflow from an authorised operator environment. Migrations remain explicit.']);
box(650,585,480,205,'Deploy the pinned revision',['Dry-run first; strict checks before release. VPS fetches the exact pushed commit. Preserve timer states. Verify serving revision and hosted checks.']);
box(70,585,480,205,'Review real usefulness',['The owner checks the result. Real scheduled execution, data coverage and human usefulness are separate from passing tests. Problems feed the next attended change.']);
arrow([[1470,450],[1470,585]],'admin');arrow([[1230,680],[1130,680]],'admin');arrow([[650,680],[550,680]],'admin');
arrow([[70,680],[20,680],[20,335],[70,335]],'admin');
box(70,870,1640,145,'Not implemented • no autonomous software-improvement loop',['No scheduled model-powered incident diagnosis, weekly improvement reviewer, automatic code fix, commit/push or deployment. Scheduled model sessions have no shell or record-write tools. The attended VPS gateway permits requested record work only; the deploy key is read-only. The caretaker reports failures but does not repair them.'],'warning');
text(40,1070,'Recovery is attended: inspect evidence, obtain any necessary authority, repair or roll back compatible code, then verify the live result.',20,colors.muted);
}
const intake=diagram('data-to-site','From source to site | collection, filtering and visibility','30 October 2026 • Implementation map with release status below • A collected advert is not automatically a recommended role',2100,2430);
{
const {box,arrow,zone,text}=intake;
zone(40,180,2020,300,'1  GATHER • three supported entry paths');
box(70,245,600,200,'LinkedIn job-alert emails',[
 'Gmail forwards the exact job-alert sender to Cloudflare Email Routing. The Worker commits raw MIME + hash to a private D1 buffer. Every 15 minutes the VPS stores a durable spool copy, then acknowledges receipt.'
]);
box(750,245,600,200,'Configured external sources',[
 'Daily VPS discovery, about 05:30 UTC: employer boards, Adzuna/Reed, Hacker News, tenders, awards, funding and company records. Only configured sources; approved keys, request budgets and page limits apply.'
]);
box(1430,245,600,200,'Reviewed and human inputs',[
 'Incoming roles, recruiter/referral evidence, LinkedIn exports, researched findings and recorded applications. Use the existing preview/import or record gateway. Recruiter mail is not automatically read by the job-alert filter.'
],'warning');
zone(40,560,2020,320,'2  NORMALISE + FILTER • collection and candidacy are different decisions');
box(70,625,600,220,'Parse and retain source observations',[
 'Parse email cards or adapter results into source IDs, canonical URLs, original dates and stated facts. Retain source observations and read receipts. Malformed mail stays quarantined. No invented pay, publication date or vacancy confirmation.'
],'data');
box(750,625,600,220,'Vacancy and tender admission checks',[
 'Jobs: title scope, location, negative roles, domain, employer exclusions, stated pay and closure/expiry. Tenders use source-specific rules. Unknown stays unknown. Shared job-title equivalents are built, awaiting release.'
]);
box(1430,625,600,220,'Retained evidence; no new job',[
 'Rejected titles/terms, malformed input and capacity deferrals are different outcomes, with reasons in observations, quarantine or import receipts. Board/feed adverts with unmatched titles keep identity, lifecycle and stated terms, not full text, unless a record holds them (awaiting release).'
],'warning');
arrow([[370,445],[370,515],[55,515],[55,735],[70,735]]);
arrow([[1050,445],[1050,515],[370,515]]);
arrow([[670,735],[750,735]]);
arrow([[1350,735],[1430,735]]);
zone(40,990,2020,320,'3  SAVE • one canonical record system');
box(70,1055,600,220,'Automatic discovery capacity',[
 'Eligible new jobs/tenders compete for a bounded working set: ten visible places, a thirty-candidate research pool and at most sixty automatic additions per rolling seven days. Over-limit evidence stays pending. Existing matches can still receive evidence.'
]);
box(750,1055,600,220,'Canonical import and record writes',[
 'Resolve source identity, advert URL and organisation. Preserve archives, human edits and workflow state. Check revisions and write atomically. Reviewed/manual intake uses its own validation, not the automatic discovery allowance. Safe replays do not duplicate records.'
]);
box(1430,1055,600,220,'Authoritative SQLite on the VPS',[
 'Jobs, tenders, people and organisations; sourced observations, checks and history. Funding/awards are organisation evidence, not invented vacancies. Application status is recorded from an actual action. Stored records and evidence are distinct from the visible shortlist.'
],'data');
arrow([[1050,845],[1050,900],[55,900],[55,1165],[70,1165]],'flow','Eligible jobs / tenders',130,922);
arrow([[370,845],[370,955],[800,955],[800,1055]],'flow','Organisation signals: type-specific rules',410,944);
arrow([[2030,345],[2078,345],[2078,970],[1050,970],[1050,1055]],'admin','Reviewed record writes',1510,955);
arrow([[670,1165],[750,1165]]);
arrow([[1350,1165],[1430,1165]]);
zone(40,1430,2020,345,'4  RESEARCH + RANK + DISPLAY • separate, linked operations');
box(70,1495,600,230,'Focused public-evidence research',[
 '09:00 / 19:00 Europe/London. Active/shortlisted work and selected discovery targets, within the existing allowance. Codex reads public evidence; a trusted worker validates and imports supported facts/checks. Blocked reads remain gaps. The model cannot write directly.'
],'research');
box(750,1495,600,230,'Derived assessment and selection',[
 'On read, shared rules assess remit, requirements, terms and evidence freshness. Selection is derived; viewing does not rewrite records or trigger research. The site and research worker use the same maintained opportunity selection. Saved/active work is treated separately.'
]);
box(1430,1495,600,230,'Private site • browser / phone',[
 'Cloudflare Access + Tunnel → VPS API/site. Reload after data updates. This week shows selected work; Jobs shows processes and folded reference/history; search finds retained records. Source/email/research panels expose receipts, failures and gaps.'
],'data');
arrow([[1730,1275],[1730,1385],[1050,1385],[1050,1495]]);
arrow([[750,1605],[670,1605]],'research');
arrow([[70,1605],[20,1605],[20,1350],[800,1350],[800,1275]],'research','Validated evidence returns to the same writer',250,1338);
arrow([[1350,1605],[1430,1605]]);
zone(40,1830,2020,320,'5  WHY A ROLE CAN BE MISSING • inspect the boundary, not just the final page');
box(70,1895,600,220,'Not collected',[
 'No matching forwarded alert, an untracked employer, a capped source page or a failed read. Gmail forwarding is not retrospective. Collection is limited to configured sources; an empty successful run does not prove complete market coverage.'
],'warning');
box(750,1895,600,220,'Collected, but not admitted',[
 'A title/location/factual rule rejected it, parsing quarantined it, or the automatic allowance deferred it. Inspect original evidence and the recorded reason. Changing a rule is not proof that every historical rejection has been recovered.'
],'warning');
box(1430,1895,600,220,'Saved, but not featured',[
 'A record may be retained as reference, need verification, sit below the selected working set or belong to history. Search and the Jobs reference view can still find it. Candidate, confirmed-open vacancy and recommended opportunity are different states.'
],'warning');
box(70,2190,1960,150,'30 OCTOBER STATUS • distinguish the live recovery from the pending software release',[
 'The historical alert backfill and Hyperlayer application record are live. Shared title-family matching and the new public Rippling/Hyperlayer reader are implemented and tested but await an explicitly authorised deployment. Code push alone changes no live scanner behavior. Ordinary record updates need no deployment. This diagram is documentation, not live telemetry.'
],'warning');
text(70,2383,'Owners: scan_alerts / scan_* → record_imports + discovery_intake → records / SQLite → relevance + work_selection → site',19,colors.muted);
}
// Restricted hosts can supply their approved executable, libraries and fonts.
const launchOptions=process.env.WO_DIAGRAM_CHROMIUM_OPTIONS?require(process.env.WO_DIAGRAM_CHROMIUM_OPTIONS):{};
const browser=await launchBrowser(chromium, {headless:true,...launchOptions,chromiumSandbox:true});
try{for(const d of [full,email,research,lifecycle,intake])await d.render(browser);}finally{await browser.close();}
