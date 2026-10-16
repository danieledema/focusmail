import os
import tempfile
os.environ['DATA_DIR'] = tempfile.mkdtemp(prefix='focusmail-test-')
os.environ['DEMO_MODE'] = 'true'
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from email import message_from_string
from fastapi.testclient import TestClient
from sqlalchemy import select
from backend.main import app, synchronize
from backend.database import Session, engine, Base
from backend.models import Thread, PendingDelete, Account, Message
from backend.auth import fernet, valid_cookie, signature
from backend.services.snooze_scheduler import snooze_time
from backend.services.thread_builder import ingest, text_body
import time
import pytest

@pytest.fixture
def client():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with TestClient(app) as client:
        yield client

def current(client):
    return client.get('/api/state?session_id=test').json()

def action(client, action, **kwargs):
    return client.post(f'/api/threads/{current(client)["thread"]["id"]}/action', json={'action':action,'session_id':'test',**kwargs})

def test_queue_archive_and_duplicate_protection(client):
    state=current(client)
    assert state['counts']['inbox']==8
    thread_id=state['thread']['id']
    r=action(client,'archive')
    assert r.status_code==200
    assert r.json()['game']['score']==2
    assert current(client)['counts']['inbox']==7
    assert client.post(f'/api/threads/{thread_id}/action',json={'action':'archive','session_id':'test'}).status_code==409
    assert len(client.get('/api/threads?status=archived').json())==1

def test_delete_undo_restores_score_and_queue(client):
    result=action(client,'delete').json()
    assert current(client)['game']['score']==1
    assert current(client)['counts']['inbox']==7
    assert client.post('/api/undo/'+result['undo_token']).status_code==200
    assert current(client)['game']['score']==0
    assert current(client)['game']['daily_processed']==0
    assert current(client)['counts']['inbox']==8
    assert client.post('/api/undo/'+result['undo_token']).status_code==409

def test_deferred_delete_survives_new_session_and_finalizes(client):
    token=action(client,'delete').json()['undo_token']
    with Session() as db:
        pending=db.get(PendingDelete,token)
        thread_id=pending.thread_id
        pending.due=time.time()-1
        db.commit()
    synchronize()
    assert client.post('/api/undo/'+token).status_code==409
    with Session() as db:
        assert db.get(Thread,thread_id).status=='deleted'
        assert not db.get(PendingDelete,token)

def test_snooze_durable_and_resurfaces_first(client):
    tid=current(client)['thread']['id']
    assert action(client,'snooze',preset='hour').status_code==200
    assert current(client)['game']['score']==-1
    with Session() as db:
        t=db.get(Thread,tid)
        assert t.status=='snoozed' and t.snoozed_until>time.time()
        t.snoozed_until=time.time()-1;db.commit()
    assert current(client)['thread']['id']==tid
    assert current(client)['counts']['snoozed']==0

def test_reply_validation_and_saved_message(client):
    assert action(client,'reply',body='  ').status_code==400
    assert current(client)['counts']['inbox']==8
    response=action(client,'reply',body='Tuesday at 10 works for me!').json()
    assert response['game']['score']==3
    assert 'first_reply' in response['game']['new_badges']
    sent=client.get('/api/threads?status=replied').json()
    assert sent[0]['messages'][-1]['body']=='Tuesday at 10 works for me!'
    assert client.get('/api/state?session_id=fresh').json()['game']['score']==0

def test_goal_inbox_zero_and_session_badges(client):
    for _ in range(8): assert action(client,'archive').status_code==200
    result=current(client)
    assert result['thread'] is None
    assert result['game']['progress']==1
    assert result['game']['streak']==1
    assert result['game']['goal_met']
    assert next(b for b in result['game']['badges'] if b['key']=='zero')['unlocked']
    assert next(b for b in result['game']['badges'] if b['key']=='speed')['unlocked']

def test_preferences_validation_and_secret_redaction(client):
    assert client.put('/api/preferences',json={'daily_goal':0}).status_code==422
    assert client.put('/api/preferences',json={'daily_goal':4,'poll_interval':2,'ai_model':'gpt-4o-mini'}).status_code==200
    state=current(client)
    assert state['preferences']['daily_goal']==4
    assert 'password' not in state['accounts'][0]
    with Session() as db:
        account=db.scalar(select(Account))
        assert account.password!='demo'
        assert fernet.decrypt(account.password.encode())==b'demo'

def test_demo_chat_and_account_guard(client):
    r=client.post('/api/ai/chat',json={'thread_id':current(client)['thread']['id'],'message':'Draft a polite reply','history':[]})
    assert r.status_code==200 and r.json()['draft']
    assert client.post('/api/accounts',json={'name':'Test','username':'you@example.com','password':'secret','imap_host':'imap.example.com','smtp_host':'smtp.example.com'}).status_code==400
    assert client.post('/api/ai/chat',json={'thread_id':999,'message':'hello'}).status_code==404

def test_csrf_rejected(client):
    assert client.post('/api/sync',headers={'Origin':'https://attacker.example'}).status_code==403

def test_cookie_signature_and_expiry():
    stamp=str(int(time.time()))
    assert valid_cookie(stamp+'.'+signature(stamp))
    assert not valid_cookie(stamp+'.invalid')
    old=str(int(time.time())-604801)
    assert not valid_cookie(old+'.'+signature(old))

def test_snooze_dates_and_dst():
    zone=ZoneInfo('Europe/London')
    now=datetime(2026,10,16,18,tzinfo=zone)
    assert datetime.fromtimestamp(snooze_time('today',now=now),zone)==datetime(2026,10,17,17,tzinfo=zone)
    assert datetime.fromtimestamp(snooze_time('weekend',now=now),zone)==datetime(2026,10,17,9,tzinfo=zone)
    assert datetime.fromtimestamp(snooze_time('week',now=now),zone)==datetime(2026,10,19,9,tzinfo=zone)
    dst=datetime(2026,10,24,12,tzinfo=zone)
    assert datetime.fromtimestamp(snooze_time('tomorrow',now=dst),zone)==datetime(2026,10,25,9,tzinfo=zone)
    assert snooze_time('hour',now=dst)-dst.timestamp()==3600

def test_thread_grouping_and_html_safety(client):
    with Session() as db:
        account=db.scalar(select(Account))
        raw=message_from_string('Subject: First topic\nFrom: Jules <jules@example.com>\nMessage-ID: <first@example.com>\nContent-Type: text/html; charset=utf-8\n\n<p>Hello</p><script>steal()</script><p>World</p>')
        assert 'steal()' not in text_body(raw)
        ingest(db,account,raw,'101','1');db.commit()
        parent=db.scalar(select(Message).where(Message.message_id=='<first@example.com>'))
        reply=message_from_string('Subject: A changed subject\nFrom: Jules <jules@example.com>\nMessage-ID: <second@example.com>\nIn-Reply-To: <first@example.com>\n\nReply content')
        ingest(db,account,reply,'102','1');db.commit()
        child=db.scalar(select(Message).where(Message.message_id=='<second@example.com>'))
        assert child.thread_id==parent.thread_id
        ingest(db,account,reply,'102','1');db.commit()
        assert len(list(db.scalars(select(Message).where(Message.message_id=='<second@example.com>'))))==1

def test_sent_history_does_not_enter_queue(client):
    with Session() as db:
        account=db.scalar(select(Account))
        sent=message_from_string('Subject: Sent-only conversation\nFrom: Alex <alex@hey.com>\nTo: Jules <jules@example.com>\nMessage-ID: <sent-only@example.com>\n\nAn outgoing note')
        ingest(db,account,sent,'sent:301','1',folder='Sent');db.commit()
        m=db.scalar(select(Message).where(Message.message_id=='<sent-only@example.com>'))
        assert db.get(Thread,m.thread_id).status=='replied'
        incoming=message_from_string('Subject: Re: Sent-only conversation\nFrom: Jules <jules@example.com>\nMessage-ID: <incoming@example.com>\nIn-Reply-To: <sent-only@example.com>\n\nThanks for the note')
        ingest(db,account,incoming,'302','1');db.commit()
        assert db.get(Thread,m.thread_id).status=='inbox'

class FakeIMAP:
    capabilities=(b'IMAP4rev1', b'MOVE')
    def __init__(self): self.calls=[]
    def select(self,*args,**kwargs): return 'OK',[b'1']
    def response(self,*args): return 'OK',[b'1']
    def list(self,*args): return 'OK',[b'(\\HasNoChildren \\Archive) "/" "Archive"',b'(\\HasNoChildren \\Trash) "/" "Deleted Items"']
    def uid(self,*args): self.calls.append(args); return 'OK',[b'']
    def logout(self): pass

def test_imap_move_uses_special_folders_and_scoped_uids(client,monkeypatch):
    from backend.services import imap_service
    fake=FakeIMAP();monkeypatch.setattr(imap_service,'connect',lambda account:fake)
    with Session() as db:
        thread=db.scalar(select(Thread))
        db.add(Message(thread_id=thread.id,account_id=thread.account_id,uid='77',uidvalidity='1',folder='INBOX',body='test'))
        db.add(Message(thread_id=thread.id,account_id=thread.account_id,uid='sent:78',uidvalidity='1',folder='Sent',body='test'))
        db.commit();imap_service.move_thread(db,thread,'delete')
        assert ('MOVE','77','"Deleted Items"') in fake.calls
        assert not any('sent:78' in str(c) for c in fake.calls)
        assert not any(c[0]=='EXPUNGE' for c in fake.calls)

def test_imap_uidplus_fallback_never_expunge_other_mail(client,monkeypatch):
    from backend.services import imap_service
    fake=FakeIMAP();fake.capabilities=(b'UIDPLUS',)
    monkeypatch.setattr(imap_service,'connect',lambda account:fake)
    with Session() as db:
        thread=db.scalar(select(Thread))
        db.add(Message(thread_id=thread.id,account_id=thread.account_id,uid='77',uidvalidity='1',body='test'));db.commit()
        imap_service.move_thread(db,thread,'archive')
        assert ('COPY','77','"Archive"') in fake.calls
        assert ('EXPUNGE','77') in fake.calls

def test_failed_archive_keeps_thread_and_score(client,monkeypatch):
    import backend.main as main
    monkeypatch.setattr(main,'DEMO',False)
    monkeypatch.setattr(main.imap_service,'sync_account',lambda *args:None)
    def fail(*args): raise RuntimeError('offline')
    monkeypatch.setattr(main.imap_service,'move_thread',fail)
    assert action(client,'archive').status_code==502
    assert current(client)['counts']['inbox']==8
    assert current(client)['game']['score']==0

def test_sent_reply_cannot_resend_after_archive_failure(client,monkeypatch):
    import backend.main as main
    monkeypatch.setattr(main,'DEMO',False)
    monkeypatch.setattr(main.imap_service,'sync_account',lambda *args:None)
    sent=[]
    def send(*args): sent.append(args);return '<actually-sent@example.com>'
    def fail(*args): raise RuntimeError('offline')
    monkeypatch.setattr(main.smtp_service,'send_reply',send)
    monkeypatch.setattr(main.imap_service,'move_thread',fail)
    tid=current(client)['thread']['id']
    payload={'action':'reply','body':'Hello, Jamie','session_id':'test'}
    response=client.post(f'/api/threads/{tid}/action',json=payload)
    assert response.status_code==200 and response.json()['warning']
    assert client.post(f'/api/threads/{tid}/action',json=payload).status_code==409
    assert len(sent)==1

def test_failed_smtp_does_not_consume_thread(client,monkeypatch):
    import backend.main as main
    monkeypatch.setattr(main,'DEMO',False)
    monkeypatch.setattr(main.imap_service,'sync_account',lambda *args:None)
    def fail(*args): raise RuntimeError('offline')
    monkeypatch.setattr(main.smtp_service,'send_reply',fail)
    assert action(client,'reply',body='Hello').status_code==502
    assert current(client)['counts']['inbox']==8
    assert current(client)['game']['score']==0

def test_basic_auth_and_cookie(client,monkeypatch):
    import backend.auth as auth
    monkeypatch.setattr(auth,'DEMO',False);monkeypatch.setattr(auth,'PASSWORD','test-app-password')
    client.cookies.clear()
    assert client.get('/api/state').status_code==401
    assert client.get('/api/state',auth=('anyone','wrong')).status_code==401
    assert client.get('/api/state',headers={'Authorization':'Basic not-base64'}).status_code==401
    response=client.get('/api/state',auth=('anyone','test-app-password'))
    assert response.status_code==200
    assert 'HttpOnly' in response.headers['set-cookie']
    assert 'SameSite=strict' in response.headers['set-cookie']
    assert client.get('/api/state').status_code==200


def test_streak_and_all_achievement_thresholds(client):
    from backend.models import ActionEvent
    from backend.services.game_service import today, game_state
    with Session() as db:
        for n in range(100):
            db.add(ActionEvent(thread_id=1, session_id='test',action='reply',points=3,at=time.time()-n,day=(today()-timedelta(days=n%7)).isoformat()))
        db.flush()
        state=game_state(db,'test',unlock=True)
        assert state['streak']==7
        assert all(next(b for b in state['badges'] if b['key']==key)['unlocked'] for key in ['first_reply','decisive','streak_3','streak_7','speed','century'])
