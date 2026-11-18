"""Fixed structured outputs accepted from the research model."""
def object_schema(properties):
    return dict(type='object',properties=properties,required=list(properties),additionalProperties=False)
STRING={'type':'string'}
DAILY_REVIEW=object_schema({'report':STRING,'thread_id':STRING})
STRINGS={'type':'array','items':STRING,'maxItems':6}
CLAIM=object_schema({'status':{'type':'string','enum':['confirmed','unverified','closed']},'source':{'type':'integer'},'quote':STRING,'basis':STRING})
FINDING=object_schema({'vacancy':CLAIM,'contacts':{'type':'array','items':object_schema({'person_id':STRING,'claim':CLAIM}),'maxItems':12},
    'facts':{'type':'array','items':object_schema({'text':STRING,'source':{'type':'integer'},'quote':STRING}),'maxItems':6},
    'fit':STRING,'gaps':STRINGS,'public_contact_route':STRING})
DISCOVERY=object_schema({'urls':{'type':'array','items':STRING,'maxItems':4}})
BRIEF=object_schema({
    'vacancy': CLAIM,
    'contacts': {'type':'array','items':object_schema({'person_id':STRING,'claim':CLAIM}),'maxItems':12},
    'candidates': {'type':'array','maxItems':8,'items':object_schema({
        'subject_id':STRING,'kind':{'type':'string','enum':['employer','role','context','route','vacancy']},
        'value':STRING,'source':{'type':'integer'},'quote':STRING})},
    'unknowns':STRINGS})
# One weekly approach: route claims for the supplied routes plus a reason, ask and complete message,
# or an explicit needs_information. The worker validates every part before anything is saved.
PREPARATION=object_schema({'status':{'type':'string','enum':['ready','needs_information']},
    'contacts':{'type':'array','items':object_schema({'person_id':STRING,'claim':CLAIM}),'maxItems':4},
    'reason':STRING,'ask':STRING,'message':STRING,
    'facts':{'type':'array','items':object_schema({'text':STRING,'source':{'type':'integer'},'quote':STRING}),'maxItems':6},
    'unknowns':STRINGS})
