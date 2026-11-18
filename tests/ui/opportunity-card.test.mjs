import test from 'node:test';
import assert from 'node:assert/strict';
import {cardExplanation, sourcePresentation, cardRemit} from '../../workspace/js/opportunity-card.js';
import {requirementPresentation} from '../../workspace/js/approach-brief.js';
import {companyInitials} from '../../workspace/js/company-logo.js';
import {summaryRefreshDecision} from '../../workspace/js/summary-refresh.js';
import {jobDecisionFacts, jobLocation} from '../../workspace/js/record-presentation.js';

test('job location keeps explicit workplace and source attendance wording visible',()=>{
 for (const [record,expected] of [
  [{location:'London, Bath or Edinburgh · at least once per week',workplace:'hybrid'},'London, Bath or Edinburgh · at least once per week · Hybrid'],
  [{location:'London',workplace:'onsite'},'London · On-site'],
  [{location:'UK',workplace:'remote'},'UK · Remote'],
  [{location:'London (Hybrid)',workplace:'hybrid'},'London (Hybrid)'],
  [{location:'Remote UK',workplace:'remote'},'Remote UK'],
  [{location:'London · on site',workplace:'onsite'},'London · on site'],
  [{workplace:'hybrid'},'Location not stated · Hybrid'],
  [{location:'Remote village',workplace:'unknown'},'Remote village'],
  [{},'Location not stated'],
 ]) {
  const before=structuredClone(record);
  assert.equal(jobLocation(record),expected);
  assert.equal(jobDecisionFacts(record).location,expected);
  assert.deepEqual(record,before);
 }
});

test('job decision facts retain fit gaps and qualified terms without inventing fit from a title',()=>{
 const record={engagement:'contract',relevance:{reasons:[{text:'Leadership target: Engineering Manager',weight:40}],
  qualification:{engagement:'unknown',conflicts:[]},fit:{gaps:['Sector experience is unconfirmed.'],evidence:[]}}};
 const before=structuredClone(record),facts=jobDecisionFacts(record);
 assert.equal(facts.engagement,'Engagement not stated');
 assert.equal(facts.fit,'Not established from recorded evidence.');
 assert.equal(facts.uncertainty,'Sector experience is unconfirmed.');
 assert.deepEqual(facts.missing,['work and requirements not established','pay and engagement not stated']);
 assert.deepEqual(record,before);
 const rich={...record,pay:'£95,000',summary:'Lead software teams.',relevance:{...record.relevance,
  reasons:[{text:'Supported software leadership remit',weight:60,fit:true}],fit:{gaps:[]}}};
 assert.equal(jobDecisionFacts(rich).fit,'Supported software leadership remit');
 assert.deepEqual(jobDecisionFacts(rich).missing,[]);
});

test('card explanation starts with the actual selection factor and keeps uncertainty separate',()=>{
 const record={relevance:{fit:{gaps:['Essential sector experience is unconfirmed.']},reasons:[{text:'Fresh advert',weight:10},{text:'Supported leadership remit',weight:60,fit:true}]}};
 assert.deepEqual(cardExplanation(record,{reason:'Supported leadership remit'}),{reasons:['Supported leadership remit','Fresh advert'],uncertainty:'Essential sector experience is unconfirmed.'});
});
test('source receipt date never comes from a record edit or company logo',()=>{
 const record={source_file:'source:alert:linkedin',date:'2026-10-28',url:'https://www.linkedin.com/jobs/view/1/',company_logo_id:'C'};
 assert.equal(sourcePresentation(record,{}).at,'');
 assert.deepEqual(sourcePresentation(record,{source:{label:'LinkedIn email',at:'2026-10-25T10:00:00Z',url:record.url,basis:'receipt'}}),{label:'LinkedIn email',at:'2026-10-25T10:00:00Z',url:record.url,basis:'receipt'});
});
test('company initials are a bounded text fallback',()=>{
 assert.equal(companyInitials('Alderbridge Digital'),'AD');assert.equal(companyInitials('Morrow'),'M');assert.equal(companyInitials(''),'—');
});
test('card remit uses only an attributed duty and preserves the original quote and source',()=>{
 const summary='Hybrid, based in London. Full source context.',quote='Lead software teams; improve release readiness.';
 const url='https://fixture.example/role?source=advert#duties';
 const duty={kind:'remit',quote,url},requirement={...duty,kind:'requirement',quote:'Quality engineering experience is essential.'};
 const record=evidence=>({summary,relevance:{fit:{evidence}}});
 const selected=record([requirement,duty]),before=structuredClone(selected);
 assert.deepEqual(cardRemit(selected),{text:quote,url});assert.deepEqual(selected,before);
 for(const evidence of [[requirement],[{...duty,kind:undefined}],[{...duty,url:''}],[{...duty,url:'javascript:alert(1)'}],[{...duty,quote:'   '}],[]])
  assert.deepEqual(cardRemit(record(evidence)),{text:'Hybrid, based in London.',url:''});
});
test('a background summary never replaces an edited or older snapshot',()=>{
 assert.equal(summaryRefreshDecision({baseVersion:'a',currentVersion:'b',nextVersion:'c'}),'discard');
 assert.equal(summaryRefreshDecision({baseVersion:'a',currentVersion:'a',nextVersion:'a'}),'unchanged');
 assert.equal(summaryRefreshDecision({baseVersion:'a',currentVersion:'a',nextVersion:'b',editing:true}),'pending');
 assert.equal(summaryRefreshDecision({baseVersion:'a',currentVersion:'a',nextVersion:'b'}),'apply');
});

test('requirement comparison labels preserve missing evidence and source negation',()=>{
 assert.deepEqual(requirementPresentation({quote:'NHS experience',importance:'essential',status:'not_established'}),
  {quote:'NHS experience',importance:'Essential',status:'Not established from recorded experience'});
 assert.equal(requirementPresentation({quote:'No degree required',importance:'ambiguous',status:'not_required',negated:true}).status,'Not required by this source');
 assert.equal(requirementPresentation({importance:'desirable',status:'supported'}).importance,'Desirable');
 assert.equal(requirementPresentation({importance:'essential',status:'conflicting'}).status,'Source wording conflicts');
});
