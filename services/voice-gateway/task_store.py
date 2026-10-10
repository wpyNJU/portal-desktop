import sqlite3,time,uuid,re,json
from task_titles import task_title

class TaskStore:
    def __init__(self,path):
        self.db=sqlite3.connect(str(path));self.db.row_factory=sqlite3.Row
        self.db.execute("CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, scope TEXT NOT NULL, title TEXT, state TEXT, text TEXT, cursor INTEGER, delivery TEXT, created REAL, updated REAL)")
        self.db.execute('CREATE TABLE IF NOT EXISTS task_requests (scope TEXT, request_id TEXT, task_id TEXT, fingerprint TEXT, PRIMARY KEY(scope,request_id))')
        self.db.execute("CREATE TABLE IF NOT EXISTS task_intents (task_id TEXT PRIMARY KEY, intent TEXT, parent_id TEXT)")
        if 'request' not in {r['name'] for r in self.db.execute('PRAGMA table_info(tasks)')}:
            self.db.execute("ALTER TABLE tasks ADD COLUMN request TEXT NOT NULL DEFAULT ''")
            self.db.execute("UPDATE tasks SET request=COALESCE(title,'')")
        if 'requested_at' not in {r['name'] for r in self.db.execute('PRAGMA table_info(tasks)')}:
            self.db.execute("ALTER TABLE tasks ADD COLUMN requested_at REAL NOT NULL DEFAULT 0")
            self.db.execute("UPDATE tasks SET requested_at=COALESCE(created,0)")
        columns={r['name'] for r in self.db.execute('PRAGMA table_info(tasks)')}
        for column in ('error_code','error_message'):
            if column not in columns:self.db.execute(f"ALTER TABLE tasks ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
        for column,definition in (('stage',"TEXT NOT NULL DEFAULT ''"),('stage_updated','REAL NOT NULL DEFAULT 0'),('summary',"TEXT NOT NULL DEFAULT ''")):
            if column not in columns:self.db.execute(f'ALTER TABLE tasks ADD COLUMN {column} {definition}')
        self.db.commit()
    def create(self,scope,message,title=None):
        key=uuid.uuid4().hex;now=time.time()
        self.db.execute('INSERT INTO tasks (id,scope,title,state,text,cursor,delivery,created,updated,request,requested_at,stage,stage_updated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                        (key,scope,task_title(message,title),'running','',0,'waiting',now,now,message,now,'queued',now));self.db.commit();return key
    def record(self,row):
        value=dict(row);request=value.pop('request','');title=value.get('title')
        value['title']=task_title(request,None if title==request else title)
        meta=self.db.execute('SELECT intent,parent_id FROM task_intents WHERE task_id=?',(value['id'],)).fetchone()
        if meta:value.update(intent=json.loads(meta['intent']),parent_id=meta['parent_id'])
        return value
    def request_match(self,scope,request_id,fingerprint):
        row=self.db.execute('SELECT task_id,fingerprint FROM task_requests WHERE scope=? AND request_id=?',(scope,request_id)).fetchone()
        if not row:return None
        if row['fingerprint']!=fingerprint:raise ValueError('request_id conflict')
        return row['task_id']
    def bind_request(self,scope,request_id,key,fingerprint):
        self.db.execute('INSERT INTO task_requests VALUES (?,?,?,?)',(scope,request_id,key,fingerprint));self.db.commit()
    def running_intent(self,scope,intent,parent_id):
        row=self.db.execute("SELECT t.id FROM tasks t JOIN task_intents i ON t.id=i.task_id WHERE t.scope=? AND t.state='running' AND i.intent=? AND i.parent_id=? LIMIT 1",(scope,intent,parent_id)).fetchone()
        return row['id'] if row else None
    def set_intent(self,key,intent,parent_id):
        self.db.execute('INSERT INTO task_intents VALUES (?,?,?)',(key,intent,parent_id));self.db.commit()
    def get(self,scope,key=None):
        row=self.db.execute('SELECT * FROM tasks WHERE scope=? '+('AND id=?' if key else 'ORDER BY requested_at DESC,created DESC,rowid DESC LIMIT 1'),(scope,key) if key else (scope,)).fetchone()
        if not row:raise ValueError('没有找到这条任务记录')
        value=self.record(row);value['request']=row['request'];return value
    def update(self,scope,key,**values):
        assert set(values)<= {'state','text','cursor','delivery','error_code','error_message','stage','stage_updated','summary'}
        values['updated']=time.time()
        self.db.execute('UPDATE tasks SET '+','.join(k+'=?' for k in values)+' WHERE scope=? AND id=?',(*values.values(),scope,key));self.db.commit()
    def touch(self,scope,key):
        # Only a user request changes conversational priority, never Agent progress.
        self.get(scope,key);now=time.time()
        self.db.execute('UPDATE tasks SET requested_at=MAX(requested_at,?),updated=? WHERE scope=? AND id=?',(now,now,scope,key));self.db.commit()
    def list(self,scope,offset=0):
        return [self.record(r) for r in self.db.execute('SELECT id,title,request,state,cursor,delivery,created,updated,requested_at,error_code,error_message,stage,stage_updated,summary,length(text) AS length FROM tasks WHERE scope=? ORDER BY requested_at DESC,created DESC,rowid DESC LIMIT 20 OFFSET ?',(scope,max(0,int(offset))))]
    def context_rows(self,scope,limit=12):
        # Active work is selected before recent history, without loading bodies.
        limit=min(40,max(1,int(limit)))
        return [self.record(r) for r in self.db.execute("SELECT id,title,request,state,cursor,delivery,created,updated,requested_at,error_code,error_message,stage,stage_updated,summary,length(text) AS length FROM tasks WHERE scope=? ORDER BY CASE WHEN state='running' THEN 0 ELSE 1 END,requested_at DESC,created DESC,rowid DESC LIMIT ?",(scope,limit))]
    def active_count(self,scope):
        return self.db.execute("SELECT count(*) FROM tasks WHERE scope=? AND state='running'",(scope,)).fetchone()[0]
    def watchable(self,scope,since=None):
        # Reconnect follows running work; terminal history is fetched explicitly.
        condition="(state='running' OR delivery!='reported')"
        params=[scope]
        if since is not None:
            condition+=" AND (state='running' OR created>=?)"
            params.append(since)
        return [dict(r) for r in self.db.execute("SELECT id FROM tasks WHERE scope=? AND "+condition+" ORDER BY requested_at DESC,created DESC,rowid DESC",params)]
    def context_states(self,scope):
        return [dict(r) for r in self.db.execute("SELECT id,state,stage,summary,delivery FROM tasks WHERE scope=? AND (state='running' OR delivery!='reported')",(scope,))]
    def page(self,scope,key=None,offset=0,limit=6000):
        row=self.get(scope,key);text=row.pop('text');offset=max(0,int(offset));limit=min(8000,max(1,int(limit)))
        row.update(text=text[offset:offset+limit],offset=offset,total=len(text),next_offset=offset+limit if offset+limit<len(text) else None);return row
    def resumable(self,scope):
        row=self.db.execute("SELECT id FROM tasks WHERE scope=? AND state!='running' AND cursor<length(text) AND delivery IN ('paused','asking','awaiting_choice','pending','reporting') ORDER BY requested_at DESC,updated DESC,rowid DESC LIMIT 1",(scope,)).fetchone()
        return row['id'] if row else None
    def recover(self):
        self.db.execute("UPDATE tasks SET state='interrupted',delivery='paused',stage='interrupted',stage_updated=? WHERE state='running'",(time.time(),))
        self.db.execute("UPDATE tasks SET delivery='paused' WHERE delivery IN ('reporting','pending','asking')")
        self.db.commit()

def next_segment(text,cursor,maximum=40):
    chunk=text[cursor:cursor+maximum];m=re.search(r'[。！？!?；;\n]',chunk)
    if m:chunk=chunk[:m.end()]
    return chunk,cursor+len(chunk)
