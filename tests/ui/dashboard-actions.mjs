import assert from 'node:assert/strict';
import {mkdirSync} from 'node:fs';
import {join} from 'node:path';

// Task-level regressions use named synthetic records and independently pinned memberships.
export async function verifyDashboardActions(browser, name, evidence, base) {
  for (const viewport of [{width:1280,height:900},{width:390,height:844}]) {
    const page = await browser.newPage({viewport});
    await evidence.attach(page.context());
    const navigate = async locator => Promise.all([
      page.evaluate(()=>new Promise(resolve=>window.addEventListener('hashchange',resolve,{once:true}))),
      locator.click(),
    ]);
    const errors=[],writes=[];
    page.on('pageerror', e=>errors.push(e.message));
    page.on('request', r=>{if(r.method()!=='GET'&&!r.url().includes('/api/seen/'))writes.push(r.url());});
    const state=await(await fetch(base+'/api/state?view=summary')).json();
    const jobs=state.records.filter(r=>r.type==='job').slice(0,4);
    const people=state.records.filter(r=>r.type==='person').slice(0,4);
    assert.equal(jobs.length,4); assert.equal(people.length,4);
    jobs.forEach((r,i)=>{r.name=i===0?'Apply here':`Waiting role ${i}`;});
    people.forEach((r,i)=>{r.name=['Alex Reply','Blair Reply','Casey Resolved','Drew Excluded'][i];delete r.exclusion;delete r.exclusion_contexts;r.relationship='warm';r.archived_at=null;delete r.relevance;});
    people[3].relationship='do_not_contact';
    const action=(i,progress,suffix='')=>({id:`reply:${i}${suffix}`,person_id:people[i].id,record_id:people[i].id,progress});
    state.intelligence={actions:[action(0,{status:'replied'}),action(0,{status:'replied'},':duplicate'),
      action(1,{status:'waiting',reply_to_record:true,next:'Record their reply and decide the next step.'}),
      action(2,{status:'replied',next_step:{kind:'done'}}),action(3,{status:'replied'})],research:[],outcomes_summary:{counts:{replied:3}}};
    state.opportunities={act_now:[{record_id:jobs[0].id,reason:'Shortlisted'}],
      waiting:jobs.slice(1).map(r=>({record_id:r.id,reason:'Waiting for their response'})),in_progress:[],shortlisted:[],active:[],discovery:[],needs_checking:[]};
    state.version='dashboard-actions-1';
    await page.route('**/api/state?view=summary*',r=>r.fulfill({json:state}));
    await page.goto(base+'/#dashboard');
    assert.equal(await page.title(),'work/ops');
    assert.equal(new URL(page.url()).hash,'#dashboard');
    const waiting=page.locator('.stat-tile').filter({hasText:'Waiting on others'});
    assert.equal(await waiting.locator('.stat-value').innerText(),'3');
    // Wait for the URL event: the original code revealed, then immediately folded this group again.
    await Promise.all([page.evaluate(()=>new Promise(resolve=>window.addEventListener('hashchange',resolve,{once:true}))),waiting.click()]);
    const group=page.locator('.week-waiting');
    assert.equal(await group.evaluate(n=>n.open),true,'Waiting stays open after hashchange');
    assert.equal(await group.locator(':scope > summary').evaluate(n=>n.tabIndex),0,'the disclosure stays in keyboard tab order');
    assert.deepEqual(await group.locator('[data-record-id]').evaluateAll(ns=>ns.map(n=>n.dataset.recordId)),jobs.slice(1).map(r=>r.id));
    assert.equal(await group.locator('[data-record-id="'+jobs[0].id+'"]').count(),0);
    assert.equal(new URL(page.url()).hash,'#week/section/waiting');
    assert.ok((await group.boundingBox()).y<300,'waiting destination is in the viewport');
    await page.reload(); await group.waitFor(); assert.equal(await group.evaluate(n=>n.open),true);
    await group.getByRole('button',{name:'Waiting role 1',exact:true}).click();
    await page.waitForFunction(()=>location.hash.includes('/action/')||!location.hash.includes('/section/'));
    assert.match(await page.locator('#detail[open]').innerText(),/Waiting role 1/);
    await page.locator('#close').click(); await group.waitFor();
    assert.equal(await group.evaluate(n=>n.open),true,'closing a waiting record returns to its group');
    await navigate(page.locator('[data-tab=dashboard]')); await page.locator('.dashboard').waitFor();
    await page.goBack(); await group.waitFor(); assert.equal(await group.evaluate(n=>n.open),true);
    await navigate(page.locator('[data-tab=dashboard]'));
    // Server figures replace the hunt section; measure the settled, connected rendering.
    await page.waitForFunction(()=>document.querySelector('.dashboard-figures')?.textContent.startsWith('Server figures updated'));
    const comparison=page.locator('.intake-comparison');
    const actionFirst=await page.locator('.dashboard-hunt').evaluate(node=>{
      const actions=node.querySelector(':scope > .stat-row').getBoundingClientRect();
      return actions.y<node.querySelector('.research-decisions').getBoundingClientRect().y
        && actions.y<node.querySelector('.intake-comparison').getBoundingClientRect().y;
    });
    assert.equal(actionFirst,true,'action summary precedes research and comparison');
    assert.equal(await comparison.evaluate(n=>n.tagName),'DETAILS');
    assert.equal(await comparison.evaluate(n=>n.open),false);
    assert.equal(await comparison.getByRole('button',{name:'Compare candidates',exact:true}).isVisible(),false);
    const replies=page.locator('.dashboard-replies');
    await replies.waitFor();
    assert.match(await replies.innerText(),/Outstanding replies \(2\)/);
    assert.deepEqual(await replies.getByRole('button').allTextContents(),['Alex Reply','Blair Reply']);
    assert.doesNotMatch(await replies.innerText(),/Casey Resolved|Drew Excluded/);
    const outcomes=page.locator('.dashboard-panel').filter({has:page.getByRole('heading',{name:'Recorded outcomes',exact:true})});
    assert.equal(await outcomes.getByRole('row').filter({hasText:'Replied'}).locator('td').last().innerText(),'3','historical replies remain separate');
    for(const [i,person] of people.slice(0,2).entries()) {
      await replies.getByRole('button',{name:person.name,exact:true}).click();
      await page.waitForFunction(id=>location.hash.includes(encodeURIComponent(id)),person.id);
      assert.equal(new URL(page.url()).hash,`#dashboard/${encodeURIComponent(person.id)}/action/${encodeURIComponent(`reply:${i}`)}`);
      assert.match(await page.locator('#detail[open]').innerText(),new RegExp(person.name));
      await page.locator('#close').click();
    }
    const capture=process.env.WO_DASHBOARD_CAPTURE_DIR;
    if(capture){mkdirSync(capture,{recursive:true});await page.evaluate(()=>scrollTo(0,0));await page.screenshot({path:join(capture,`dashboard-${name}-${viewport.width}.png`)});}
    await page.locator('.stat-tile').filter({hasText:'In progress'}).click();
    const emptyGroup=page.locator('.week-in-progress');await emptyGroup.waitFor();
    assert.equal(await emptyGroup.evaluate(n=>n.open),true);
    assert.match(await emptyGroup.innerText(),/No records in this section right now/);
    await navigate(page.locator('[data-tab=dashboard]'));
    state.intelligence.actions=[];state.version='dashboard-actions-empty';
    await page.reload();await replies.waitFor();assert.match(await replies.innerText(),/No outstanding replies/);
    assert.equal(await replies.getByRole('button').count(),0);
    state.intelligence_error='Synthetic assessment unavailable';state.version='dashboard-actions-unavailable';
    await page.reload();await replies.waitFor();assert.match(await replies.innerText(),/Outstanding replies are unavailable/);
    assert.doesNotMatch(await replies.innerText(),/No outstanding replies|\(0\)/);
    assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
    assert.deepEqual(errors,[]);assert.deepEqual(writes,[]);
    await page.close();
    console.log(`ok dashboard actions ${name} ${viewport.width}: exact waiting set after hashchange/reload/Back, action-first layout, folded comparison, unique outstanding replies and record links, resolved/excluded/empty/unavailable states, history retained`);
  }
}
