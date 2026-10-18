import os
import tempfile
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from email import message_from_string
from fastapi.testclient import TestClient
from sqlalchemy import select
from backend.main import app, run_maintenance
from backend.database import Session, engine, Base
from backend.models import Thread, PendingDelete, Account, Message
from backend.auth import fernet, valid_cookie, signature
from backend.services.snooze_scheduler import snooze_time
from backend.services.thread_builder import ingest, text_body
import time
import pytest

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
    run_maintenance()
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
    assert client.put('/api/preferences',json={'daily_goal':4,'ai_model':'gpt-4o-mini'}).status_code==200
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

def test_startup_maintenance_and_cached_reads_never_fetch_mail(client, monkeypatch):
    import backend.main as main
    calls=[]
    monkeypatch.setattr(main,'DEMO',False)
    monkeypatch.setattr(main.imap_service,'sync_account',lambda db, account, *args:calls.append(account.id))
    # The exact maintenance function used at startup and every scheduler tick.
    main.run_maintenance()
    client.get('/api/state')
    client.get('/api/threads?status=archived')
    assert calls==[]
    assert 'poll_interval' not in current(client)['preferences']
    assert client.post('/api/sync').status_code==200
    deadline=time.monotonic()+2
    while client.get('/api/sync').json()['running'] and time.monotonic()<deadline: time.sleep(.01)
    assert len(calls)==1


def test_archive_uses_receiving_account_and_its_own_folder(client, monkeypatch):
    from backend.services import imap_service
    connected=[]
    fake=FakeIMAP()
    def connect(account):
        connected.append(account.username)
        return fake
    monkeypatch.setattr(imap_service,'connect',connect)
    with Session() as db:
        other=Account(name='Work',username='alex@work.example',password=fernet.encrypt(b'work-password').decode(),imap_host='imap.work.example',smtp_host='smtp.work.example')
        db.add(other);db.flush()
        thread=Thread(account_id=other.id,subject='A work conversation',oldest=time.time(),status='inbox')
        db.add(thread);db.flush()
        db.add(Message(thread_id=thread.id,account_id=other.id,uid='88',uidvalidity='1',body='Work email'))
        db.commit()
        imap_service.move_thread(db,thread,'archive')
        assert connected==['alex@work.example']
        assert ('MOVE','88','"Archive"') in fake.calls


def test_api_key_is_encrypted_redacted_and_persistent(client, monkeypatch):
    import backend.main as main
    from backend.models import Setting
    from backend.secrets import API_KEY_SETTING, ai_api_key
    monkeypatch.setattr(main, 'DEMO', False)
    secret = 'sk-test-only-not-a-real-key'
    response = client.put('/api/ai/key', json={'key':secret})
    assert response.status_code == 200 and response.json()['saved']
    assert secret not in response.text
    with Session() as db:
        saved = db.get(Setting, API_KEY_SETTING)
        assert saved.value != secret and fernet.decrypt(saved.value.encode()).decode() == secret
    # A fresh database session, as on restart, reads the saved credential.
    with Session() as db: assert ai_api_key(db) == secret
    state = current(client)
    assert state['ai_key_saved'] and secret not in str(state)
    assert client.put('/api/ai/key', json={'key':''}).status_code == 200
    assert not current(client)['ai_key_saved']


def test_desktop_requires_single_use_launch_token(monkeypatch):
    import backend.auth as auth
    from fastapi import FastAPI
    monkeypatch.setattr(auth, 'DESKTOP', True)
    monkeypatch.setattr(auth, 'DEMO', True)  # Even demo desktop stays private to its window.
    monkeypatch.setattr(auth, 'DESKTOP_TOKEN', 'desktop-token-for-test-only')
    monkeypatch.setattr(auth, 'PASSWORD', 'desktop-session-secret')
    desktop = FastAPI()
    desktop.add_middleware(auth.AuthMiddleware)
    @desktop.get('/api/state')
    def private(): return {'ok':True}
    with TestClient(desktop) as client:
        assert client.get('/api/state').status_code == 401
        assert client.get('/desktop/start/wrong-token').status_code == 401
        response = client.get('/desktop/start/desktop-token-for-test-only', follow_redirects=False)
        assert response.status_code == 303 and response.headers['location'] == '/'
        assert 'HttpOnly' in response.headers['set-cookie']
        assert client.get('/api/state').json() == {'ok':True}
        assert "connect-src 'self'" in client.get('/api/state').headers['content-security-policy']
        assert client.get('/desktop/start/desktop-token-for-test-only').status_code == 401
        client.cookies.clear()
        assert client.get('/api/state').status_code == 401


def test_desktop_rejects_browser_origin_mutations(monkeypatch):
    import backend.auth as auth
    from fastapi import FastAPI
    monkeypatch.setattr(auth, 'DESKTOP', True)
    monkeypatch.setattr(auth, 'DESKTOP_TOKEN', 'test-launch-token')
    desktop = FastAPI()
    desktop.add_middleware(auth.AuthMiddleware)
    with TestClient(desktop) as client:
        client.get('/desktop/start/test-launch-token', follow_redirects=False)
        response = client.put('/api/ai/key', headers={'Origin':'https://other-site.example'}, json={'key':'never-saved'})
        assert response.status_code == 403


def test_google_app_password_failure_is_actionable_and_redacted(client, monkeypatch):
    import backend.main as main
    import imaplib
    monkeypatch.setattr(main, 'DEMO', False)
    def fail(account):
        raise imaplib.IMAP4.error(b'[ALERT] Application-specific password required: do-not-echo-provider-data')
    monkeypatch.setattr(main.imap_service, 'connect', fail)
    response=client.post('/api/accounts/1/test')
    assert response.status_code==502
    detail=response.json()['detail']
    assert detail['checks'][0]['code']=='app_password_required'
    assert detail['checks'][1]['status']=='skipped'
    assert 'Google' in detail['message'] and 'app password' in detail['message']
    assert 'do-not-echo-provider-data' not in response.text
    assert 'app password' in current(client)['accounts'][0]['sync_error']


def test_password_update_preserves_account_and_encrypts_secret(client, monkeypatch):
    import backend.main as main
    monkeypatch.setattr(main, 'DEMO', False)
    before=current(client)
    response=client.put('/api/accounts/1/password',json={'password':'replacement-app-password'})
    assert response.status_code==200 and 'password' not in response.json()
    with Session() as db:
        row=db.get(Account,1)
        assert row.password!='replacement-app-password'
        assert fernet.decrypt(row.password.encode())==b'replacement-app-password'
    assert current(client)['counts']==before['counts']
    assert client.put('/api/accounts/1/password',json={'password':''}).status_code==422


def test_connection_test_distinguishes_smtp_failure(client, monkeypatch):
    import backend.main as main
    import smtplib
    monkeypatch.setattr(main, 'DEMO', False)
    class Connected:
        def noop(self): pass
        def logout(self): pass
    monkeypatch.setattr(main.imap_service,'connect',lambda _:Connected())
    def fail(_): raise smtplib.SMTPAuthenticationError(535,b'login failed: never-echo-this')
    monkeypatch.setattr(main.smtp_service,'test_smtp',fail)
    response=client.post('/api/accounts/1/test')
    checks=response.json()['detail']['checks']
    assert checks[0]['service']=='IMAP' and checks[0]['status']=='ok'
    assert checks[1]['service']=='SMTP' and checks[1]['code']=='authentication_failed'
    assert 'never-echo-this' not in response.text


def test_diagnostics_classify_dns_tls_timeout():
    import socket
    import ssl
    from backend.services.connection_diagnostics import connection_error
    assert connection_error(socket.gaierror('untrusted-hostname'))['code']=='hostname_not_found'
    assert connection_error(TimeoutError())['code']=='connection_timeout'
    assert connection_error(ssl.SSLCertVerificationError())['code']=='certificate_verification_failed'


@pytest.mark.parametrize('auth_type',['password','oauth2'])
@pytest.mark.parametrize('extension',['MOVE','UIDPLUS'])
def test_move_refreshes_capabilities_after_authentication(client, monkeypatch, auth_type, extension):
    from backend.services import imap_service
    class AuthenticatedIMAP(FakeIMAP):
        capabilities = (b'IMAP4rev1', b'AUTH=XOAUTH2')
        authenticated = False
        def login(self, *args): self.authenticated = True
        def authenticate(self, *args): self.authenticated = True
        def capability(self):
            assert self.authenticated
            return 'OK', [b'imap4rev1 ' + extension.lower().encode()]
    fake=AuthenticatedIMAP()
    monkeypatch.setattr(imap_service.imaplib,'IMAP4_SSL',lambda *args,**kwargs:fake)
    monkeypatch.setattr(imap_service,'access_token',lambda account:'fictional-token')
    with Session() as db:
        thread=db.scalar(select(Thread))
        account=db.get(Account,thread.account_id); account.auth_type=auth_type
        db.add(Message(thread_id=thread.id,account_id=account.id,uid='77',uidvalidity='1',folder='INBOX',body='test'))
        db.commit()
        imap_service.move_thread(db,thread,'delete')
    if extension == 'MOVE':
        assert ('MOVE','77','"Deleted Items"') in fake.calls
    else:
        assert ('COPY','77','"Deleted Items"') in fake.calls
        assert ('EXPUNGE','77') in fake.calls
    assert all(call != ('EXPUNGE',) for call in fake.calls)


def test_capability_failure_closes_connection_without_changing_mail(client,monkeypatch):
    from backend.services import imap_service
    from unittest.mock import Mock
    fake=Mock()
    fake.capability.return_value=('NO',[b'not available'])
    monkeypatch.setattr(imap_service.imaplib,'IMAP4_SSL',lambda *args,**kwargs:fake)
    with Session() as db:
        with pytest.raises(RuntimeError,match='authenticated IMAP capabilities'):
            imap_service.connect(db.scalar(select(Account)))
    fake.logout.assert_called_once()
    fake.uid.assert_not_called()


def test_reply_and_archive_sends_once_and_uses_receiving_account(client,monkeypatch):
    import backend.main as main
    monkeypatch.setattr(main,'DEMO',False)
    sent=[];moves=[]
    monkeypatch.setattr(main.smtp_service,'send_reply',lambda account,*args:sent.append(account.id) or '<reply-test@example.com>')
    monkeypatch.setattr(main.imap_service,'move_thread',lambda db,thread,action:moves.append((thread.account_id,action)))
    before=current(client);tid=before['thread']['id'];aid=before['thread']['account']['id']
    response=action(client,'reply',body='A test reply',archive_after_reply=True)
    assert response.status_code==200 and response.json()['reply_archived']
    assert sent==[aid] and moves==[(aid,'archive')]
    assert current(client)['counts']['inbox']==before['counts']['inbox']-1
    with Session() as db:
        assert db.get(Thread,tid).status=='replied'
        copy=db.scalar(select(Message).where(Message.message_id=='<reply-test@example.com>'))
        assert copy.folder=='Sent'
    assert client.post(f'/api/threads/{tid}/action',json={'action':'reply','body':'Duplicate','session_id':'test'}).status_code==409
    assert len(sent)==1


def test_reply_only_keeps_inbox_and_never_archives(client,monkeypatch):
    import backend.main as main
    monkeypatch.setattr(main,'DEMO',False)
    monkeypatch.setattr(main.smtp_service,'send_reply',lambda *args:'<reply-only@example.com>')
    def unexpected(*args): raise AssertionError('Send only must never archive')
    monkeypatch.setattr(main.imap_service,'move_thread',unexpected)
    before=current(client);tid=before['thread']['id']
    response=action(client,'reply',body='Keep this conversation',archive_after_reply=False)
    assert response.status_code==200 and not response.json()['reply_archived']
    assert current(client)['counts']['inbox']==before['counts']['inbox']
    with Session() as db: assert db.get(Thread,tid).status=='inbox'


def test_smtp_reply_unfolds_threading_headers():
    from backend.services import smtp_service
    from unittest.mock import Mock,patch
    account=Account(username='me@example.com',smtp_host='smtp.example.com',smtp_port=465)
    latest=Message(sender='you@example.com',message_id='\r\n <original@example.com>',references='<first@example.com>\r\n <second@example.com>')
    smtp=Mock()
    with patch.object(smtp_service.smtplib,'SMTP_SSL',return_value=smtp),patch.object(smtp_service,'authenticate'):
        smtp_service.send_reply(account,latest,'Test','Hello')
    message=smtp.send_message.call_args.args[0]
    assert message['In-Reply-To']=='<original@example.com>'
    assert message['References']=='<first@example.com> <second@example.com> <original@example.com>'


def test_gmail_archive_uses_localized_all_mail_and_ignores_local_sent_copy(client,monkeypatch):
    from backend.services import imap_service
    class Gmail(FakeIMAP):
        capabilities=(b'X-GM-EXT-1',b'MOVE',b'UIDPLUS')
        def list(self,*args): return 'OK',[b'(\\All \\HasNoChildren) "/" "[Gmail]/Tutti i messaggi"']
    fake=Gmail()
    monkeypatch.setattr(imap_service,'connect',lambda account:fake)
    with Session() as db:
        thread=db.scalar(select(Thread))
        db.add(Message(thread_id=thread.id,account_id=thread.account_id,uid='77',uidvalidity='1',folder='INBOX',body='Original'))
        db.add(Message(thread_id=thread.id,account_id=thread.account_id,uid='sent-local',uidvalidity='local',folder='Sent',body='Reply'))
        db.commit();imap_service.move_thread(db,thread,'archive')
    assert ('MOVE','77','"[Gmail]/Tutti i messaggi"') in fake.calls
    assert not any(call[0] in ('COPY','EXPUNGE') for call in fake.calls)


def test_nonexistent_folder_none_response_is_not_an_archive_destination():
    from backend.services.imap_service import destination
    class MissingFolders:
        def list(self,*args): return 'OK',[None]
    with pytest.raises(RuntimeError,match='No provider folder found'):
        destination(MissingFolders(),'archive')
