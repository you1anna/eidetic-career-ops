import test from 'node:test';
import assert from 'node:assert/strict';
import {siteProblems, freshness} from '../../workspace/js/site-status.js';
import {level, bytes} from '../../workspace/js/dashboard.js';

const now = Date.parse('2026-11-03T16:00:00Z');
const ago = minutes => new Date(now - minutes * 60000).toISOString();
// A healthy site: every run recent and complete, so nothing needs the owner.
const healthy = () => ({
  freshness: {ingest_at: ago(10 * 60), backup_at: ago(14 * 60), failures: [], capped: []},
  caretaker_processing: {last_attempt: ago(5), status: 'ok', checks: [{id: 'origin', label: 'Site health', status: 'ok', detail: ''}]},
  email_processing: {last_attempt: ago(5), status: 'ok', last_alert_processed: ago(60)},
  research_processing: {last_attempt: ago(6 * 60), status: 'ok', total: 3, checked: 3, backlog: 0, unverified: 0, contacts_total: 0, contacts_verified: 0},
});

test('a healthy site has no problems and no gaps', () => {
  assert.deepEqual(siteProblems(healthy(), [], now), {problems: [], gaps: []});
});

test('a failure card and its own caretaker check count once, as the card', () => {
  const state = healthy();
  state.caretaker_processing.checks.push({id: 'source:feed:adzuna:x', label: 'feed:adzuna:x', status: 'failed', detail: 'Read failed', subject: 'source:feed:adzuna:x'},
    {id: 'source:board:other', label: 'board:other', status: 'failed', detail: 'No card yet', subject: 'source:board:other'});
  const card = {id: 54, question: 'The Adzuna feed failed', subject: 'source:feed:adzuna:x', area: 'operations'};
  const {problems} = siteProblems(state, [card], now);
  assert.deepEqual(problems.map(p => [p.kind, p.title]), [['decision', 'The Adzuna feed failed'], ['check', 'board:other · failed']]);
});

test('partial and unknown checks are gaps, listed but not counted', () => {
  const state = healthy();
  state.caretaker_processing.checks.push({id: 'offhost', label: 'Off-host copy and restore drill', status: 'unknown', detail: 'Evidence remains on the Mac.'},
    {id: 'source:tender:cf', label: 'tender:contracts-finder', status: 'partial', detail: 'Page limit'});
  const {problems, gaps} = siteProblems(state, [], now);
  assert.equal(problems.length, 0);
  assert.deepEqual(gaps.map(g => g.title), ['Off-host copy and restore drill · unknown', 'tender:contracts-finder · partial']);
});

test('source coverage gaps use human-readable source labels and never claim a partial source has a failure card', () => {
  const state = healthy();
  state.coverage = {
    channels: [{
      id: 'boards',
      sources: [{scope: 'feed:adzuna:gb:head-of-product', label: 'Adzuna · Head of Product', summary: 'Stopped at the page limit (5 pages).'}],
    }],
  };
  state.caretaker_processing.checks.push({
    id: 'source:feed:adzuna:gb:head-of-product',
    label: 'feed:adzuna:gb:head-of-product',
    status: 'partial',
    detail: 'Source partial; its failure decision on the Dashboard holds evidence and recovery.',
  });
  const {gaps} = siteProblems(state, [], now);
  assert.deepEqual(gaps, [{
    kind: 'check',
    label: 'Adzuna · Head of Product',
    status: 'partial',
    title: 'Adzuna · Head of Product · partial',
    detail: 'Stopped at the page limit (5 pages).',
  }]);
});

test('research item failures attach affected records from issues or failure ids', () => {
  const state = healthy();
  state.records = [{id: 'job-42', name: 'VP Engineering · Example Co'}];
  state.research_processing.issues = [{id: 'job-42', outcome: 'failed', reason: 'RuntimeError', where: 'research_worker.py:312'}];
  state.research_processing.item_failures = [{reason: 'RuntimeError', where: 'research_worker.py:312', count: 1}];
  const {problems} = siteProblems(state, [], now);
  assert.equal(problems.length, 1);
  assert.deepEqual(problems[0].records, [{id: 'job-42', name: 'VP Engineering · Example Co'}]);
});

test('overdue runs, a stale daily scan and research item errors are problems', () => {
  const state = healthy();
  state.freshness.ingest_at = ago(31 * 60);
  state.email_processing.last_attempt = ago(50);
  state.research_processing.item_failures = [{reason: 'ValueError', where: 'brief_research.py:88 apply', count: 4}];
  const titles = siteProblems(state, [], now).problems.map(p => p.title);
  assert.equal(titles.length, 3);
  assert.match(titles[0], /^Daily run overdue: last ingest/);
  assert.equal(titles[1], 'Job alert email processing · Overdue — needs attention');
  assert.equal(titles[2], 'Research: 4 items failed with ValueError');
  assert.equal(siteProblems(state, [], now).problems[2].detail, 'Raised at brief_research.py:88 apply. Earlier evidence is kept.');
});

test('an unread daily run says so without inventing a time', () => {
  assert.deepEqual(freshness({}, now), {text: 'No source has been read yet.', warn: false, empty: true});
});

test('meters warn from the warning share and turn critical at 95%, unknown without a limit', () => {
  assert.equal(level(79, 100), 'ok');
  assert.equal(level(80, 100), 'warn');
  assert.equal(level(94, 100), 'warn');
  assert.equal(level(95, 100), 'danger');
  assert.equal(level(316, 512, 0.8), 'ok');      // 62%: the 3 November process peak
  assert.equal(level(507, 512, 0.8), 'danger');  // 99%: that day's service peak with file cache
  assert.equal(level(5, null), 'unknown');
  assert.equal(level(undefined, 100), 'unknown');
  assert.equal(bytes(316 * 1024 * 1024), '316 MiB');
  assert.equal(bytes(null), 'unknown');
  assert.equal(bytes(356352), '348 KiB');
});

test('a failed caretaker run counts even though its label starts with a capital', () => {
  const state = healthy();
  state.caretaker_processing.status = 'failed';  // caretakerOutcome: "Needs attention"
  assert.deepEqual(siteProblems(state, [], now).problems.map(p => p.title), ['Site caretaker · Needs attention']);
});

test('every attention wording from the email and research cards counts, in any case', () => {
  for (const [field, value, expected] of [
    ['email_processing', {last_attempt: ago(5), status: 'failed'}, 'Job alert email processing · Failed — retained mail will retry'],
    ['email_processing', {last_attempt: ago(5), status: 'partial'}, 'Job alert email processing · Incomplete — needs attention'],
    ['email_processing', {last_attempt: ago(5), status: 'ok', last_alert_processed: ago(37 * 60)}, 'Job alert email processing · No recent alerts — check delivery'],
    ['research_processing', {last_attempt: ago(60), status: 'failed'}, 'Job and contact research · Failed — needs attention'],
    ['research_processing', {last_attempt: ago(60), status: 'blocked', reason: 'access'}, 'Job and contact research · Blocked — sign-in or research service needs attention'],
  ]) {
    const state = healthy();
    state[field] = value;
    assert.deepEqual(siteProblems(state, [], now).problems.map(p => p.title), [expected]);
  }
});

test('research copy keeps operational attention separate from coverage gaps and ambiguous pauses',()=>{
 const cases=[
  [{status:'not_started'},1],
  [{last_attempt:ago(5),status:'blocked',reason:'access'},1],
  [{last_attempt:ago(5),status:'failed',reason:'usage_limit'},1],
  [{last_attempt:ago(51),status:'running'},1],
  [{last_attempt:ago(17*60),status:'ok'},1],
  [{last_attempt:ago(5),status:'partial',reason:'usage_limit',quiet_attention:true,quiet_passes:22},1],
  [{last_attempt:ago(5),status:'partial',reason:'usage_limit',pass_cap:true},0],
  [{last_attempt:ago(5),status:'partial'},0],
  [{last_attempt:ago(5),status:'blocked',reason:'disabled'},0],
 ];
 for(const [research_processing,count] of cases){const state=healthy();state.research_processing=research_processing;
  assert.equal(siteProblems(state,[],now).problems.length,count,JSON.stringify(research_processing));}
});

test('server figures say whether they are loading, current, stale or missing', async () => {
  const {serverFiguresNote} = await import('../../workspace/js/dashboard.js');
  const at = Date.parse('2026-11-03T16:05:00Z'), time = new Date(at).toLocaleTimeString('en-GB', {hour: '2-digit', minute: '2-digit'});
  assert.deepEqual(serverFiguresNote(null, {}), {text: 'Loading server figures…', level: 'muted'});
  assert.deepEqual(serverFiguresNote(null, {error: 'The site could not be reached.'}),
    {text: 'Server figures could not be loaded. The site could not be reached.', level: 'warn', retry: true});
  const stale = serverFiguresNote({}, {error: 'Boom.', updatedAt: at});
  assert.equal(stale.text, `Showing server figures from ${time}: the latest refresh failed. Boom.`);
  assert.equal(stale.retry, true);
  assert.deepEqual(serverFiguresNote({}, {updatedAt: at}), {text: `Server figures updated ${time}.`, level: 'muted'});
  assert.equal(serverFiguresNote({}, {loading: true, updatedAt: at}).text, `Refreshing server figures (last updated ${time})…`);
});

test('dashboard places job hunt first and combines current applications with 30-day moves', async () => {
  const {ORDER, applicationRows, stageRows, deferralSummary} = await import('../../workspace/js/dashboard.js');
  assert.equal(ORDER[0], 'hunt');
  const records = [
    {type: 'job', status: 'applied'},
    {type: 'job', status: 'applied'},
    {type: 'job', status: 'rejected'},
    {type: 'opportunity', stage: 'lead'},
    {type: 'opportunity', stage: 'proposal'},
  ];
  assert.deepEqual(applicationRows(records, {state: 'ok', days: 30, job_status: {applied: 2, interview: 1}}), [
    ['Applied', '2', '2'],
    ['Responded', '0', '0'],
    ['Interview', '0', '1'],
    ['Offer', '0', '0'],
    ['Rejected', '1', '0'],
    ['Closed', '0', '0'],
  ]);
  assert.deepEqual(stageRows(records), [
    ['Lead', '1'],
    ['Contacted', '0'],
    ['Conversation', '0'],
    ['Proposal', '1'],
  ]);
  assert.equal(deferralSummary({seven_day_limit: 425, role_cap: 2}), '425 seven-day limit · 2 role cap');
});

test('answerable operational choices share the Dashboard decision count; repairs do not', async () => {
  const {decisionsForYou} = await import('../../workspace/js/decisions.js');
  const choice = {id:1,status:'open',kind:'failure',area:'operations',choices:[{id:'wait'}]};
  const commercial = {id:2,status:'open',kind:'join',area:'search',choices:[{id:'yes'}]};
  assert.deepEqual(decisionsForYou([choice,commercial,{...choice,id:3,status:'answered'},
    {...choice,id:4,read_only:true},{...choice,id:5,exclusion:{reason:'Excluded'}},
    {...choice,id:6,choices:[]}]).map(d=>d.id),[1,2]);
});
