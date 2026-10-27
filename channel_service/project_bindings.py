"""Owner-authenticated goal project bindings for existing workbench model messages."""
import json
import time
from .domain import Problem, identifier, dumps


def initialize(store):
    with store.db() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS business_project_bindings(
          source TEXT NOT NULL,account TEXT NOT NULL,message_id TEXT NOT NULL,owner TEXT NOT NULL,
          goal_id TEXT NOT NULL,project_id TEXT NOT NULL,parent_issue_id TEXT NOT NULL,created REAL NOT NULL,
          PRIMARY KEY(source,account,message_id))''')


def bind(store,adapter,owner,data):
    fields={k:identifier(data.get(k),k,160 if k in ('account_id','message_id') else 80)
            for k in ('source_id','account_id','message_id','goal_id','project_id','parent_issue_id')}
    if fields['message_id']!='business-'+identifier(data.get('action_id'),'action_id',100):
        raise Problem('业务模型消息必须使用已绑定行动标识')
    project=adapter.request('GET','/api/projects/'+fields['project_id'])
    parent=adapter.issue(fields['parent_issue_id'])
    # The authenticated Multica adapter itself is scoped to the configured workspace.
    if ('HQGoal-'+fields['goal_id']+'-project') not in (project.get('description') or '') or \
       parent.get('project_id')!=fields['project_id'] or \
       ('HQGoal-'+fields['goal_id']+'-main') not in (parent.get('description') or ''):
        raise Problem('目标、项目与主任务归属不一致',403)
    with store.db(True) as db:
        if not db.execute('SELECT 1 FROM sources WHERE id=? AND enabled=1',(fields['source_id'],)).fetchone():
            raise Problem('来源不可用',403)
        prior=db.execute('SELECT * FROM business_project_bindings WHERE source=? AND account=? AND message_id=?',
                         (fields['source_id'],fields['account_id'],fields['message_id'])).fetchone()
        expected=(owner,fields['goal_id'],fields['project_id'],fields['parent_issue_id'])
        if prior and tuple(prior[k] for k in ('owner','goal_id','project_id','parent_issue_id'))!=expected:
            raise Problem('业务项目绑定不可改变',409)
        if not prior:
            db.execute('INSERT INTO business_project_bindings VALUES(?,?,?,?,?,?,?,?)',
                (fields['source_id'],fields['account_id'],fields['message_id'],*expected,time.time()))
            store.audit(db,'business.project_bound',fields['message_id'],fields['goal_id'])
    return {'bound':True,'goal_id':fields['goal_id'],'project_id':fields['project_id'],'parent_issue_id':fields['parent_issue_id']}


def apply(db,payload,route):
    row=db.execute('SELECT * FROM business_project_bindings WHERE source=? AND account=? AND message_id=?',
                   (payload['source_id'],payload['account_id'],payload['message_id'])).fetchone()
    if not row or not route:return route
    if payload.get('reply_to') or payload.get('thread_id'):
        raise Problem('目标业务推理必须建立独立项目任务',409)
    # Never read a project override from platform text/context. Only this owner-created
    # exact message binding can alter the immutable routing snapshot.
    return dict(route,project_id=row['project_id'],parent_issue_id=row['parent_issue_id'],
                business_goal_id=row['goal_id'],grouping='message',batch_seconds=0)
