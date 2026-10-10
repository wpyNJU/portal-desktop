"""Explicit task relationships and structured lookup identity, never prose similarity."""

def routing(args,call_id,store,scope):
    operation=args.get('operation')
    parent=args.get('existing_task_id') or ''
    if operation is None: # Compatibility for existing non-voice callers.
        operation='requery' if args.get('refresh') and parent else 'create'
    if operation not in ('create','requery'):raise ValueError('invalid operation')
    if operation=='create' and parent:raise ValueError('create cannot reuse parent')
    if operation=='requery':
        if not parent:raise ValueError('requery requires task ID')
        store.get(scope,parent)
    request_id=args.get('request_id',call_id)
    if not isinstance(request_id,str) or not 1<=len(request_id)<=160:raise ValueError('invalid request ID')
    intent=args.get('intent')
    if args.get('operation') is not None and intent is None:raise ValueError('intent required')
    if intent is not None:
        if not isinstance(intent,dict) or set(intent)!={'kind','subject','goal','time_range','conditions'}:raise ValueError('invalid intent')
        if intent['kind'] not in ('lookup','action'):raise ValueError('invalid kind')
        if any(not isinstance(intent[k],str) or len(intent[k])>400 for k in ('subject','goal','time_range')):raise ValueError('invalid intent field')
        if not intent['subject'].strip() or not intent['goal'].strip():raise ValueError('empty intent')
        conditions=intent['conditions']
        if not isinstance(conditions,list) or len(conditions)>20 or any(not isinstance(v,str) or len(v)>400 for v in conditions):raise ValueError('invalid conditions')
        intent={**intent,'conditions':sorted(set(v.strip() for v in conditions))}
        for k in ('subject','goal','time_range'):intent[k]=intent[k].strip()
        # Only lookups can coalesce across separate requests. Actions use the
        # explicit request ID, never an inferred equivalence of side effects.
        if intent['kind']=='action':intent=None
    return {'request_id':request_id,'intent':intent,'parent_id':parent}
