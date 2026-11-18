"""Read-only incoming-process evidence, independent of intake commands and document parsing."""
import json


def attach(conn,rs):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='incoming_receipts'").fetchone():return
    byid={r['id']:r for r in rs}
    sources={}
    for row in conn.execute('SELECT record_id,payload_json,recorded_at FROM incoming_receipts ORDER BY rowid'):
        if row['record_id'] not in byid:continue
        value=json.loads(row['payload_json'])
        source={k:value.get(k,'') for k in ('source_ref','source_text','url','origin','origin_date','actor')}
        source['recorded_at']=row['recorded_at']
        entries=sources.setdefault(row['record_id'],[])
        if any(source[k] for k in ('source_ref','source_text','url')) and not any(all(x.get(k)==source[k] for k in ('source_ref','source_text','url','origin_date')) for x in entries):entries.append(source)
        value['sources']=entries
        byid[row['record_id']]['incoming']=value
    latest={row['entity_id']:row for row in conn.execute("SELECT * FROM state_events WHERE field='status' ORDER BY id")}
    for rid,r in byid.items():
        value=r.get('incoming')
        if not value:continue
        event=latest.get(rid)
        value['current']=bool(event and event['id']==value.get('status_event_id') and event['to_value']==r.get('status'))
        if value['current']:
            value['undo_action']=dict(url=f'/api/records/{rid}/correct',method='POST',payload=dict(field='status',revision=r['revision'],note='Reverted the latest recorded process stage; source evidence retained'))
    for row in conn.execute('SELECT id,record_id,filename,kind,mime,sha,created_at FROM record_documents ORDER BY created_at,id'):
        if row['record_id'] in byid and byid[row['record_id']].get('incoming'):
            byid[row['record_id']]['incoming'].setdefault('documents',[]).append(dict(row))
