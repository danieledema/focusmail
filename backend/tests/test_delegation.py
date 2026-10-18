from types import SimpleNamespace
from unittest.mock import MagicMock
import pytest
from sqlalchemy import select
from backend.database import Session
from backend.models import Message,Thread
from backend.services import smtp_service
from backend.tests.test_app import current,action


def person(client):
    response=client.put('/api/delegates',json={'people':[{'name':'Teammate','email':'teammate@example.invalid'}]})
    assert response.status_code==200
    return response.json()['people'][0]


def test_people_persist_and_validate_recipients(client):
    saved=person(client)
    assert current(client)['delegates']==[saved]
    assert client.put('/api/delegates',json={'people':[saved,saved]}).status_code==400
    for email in ['bad','two@example.com,other@example.com','test@example.com\r\nBcc: hidden@example.com']:
        assert client.put('/api/delegates',json={'people':[{'name':'Test','email':email}]}).status_code==422
    assert current(client)['delegates']==[saved]
    assert client.put('/api/delegates',json={'people':[]}).status_code==200
    assert current(client)['delegates']==[]


def test_forward_requires_a_saved_person_and_note(client):
    saved=person(client)
    assert action(client,'delegate',body='Please handle this',delegate_id='unknown').status_code==400
    assert action(client,'delegate',body=' ',delegate_id=saved['id']).status_code==400
    assert current(client)['counts']['inbox']==8


@pytest.mark.parametrize('archive',[True,False])
def test_delegation_uses_receiving_account_and_chosen_recipient(client,monkeypatch,archive):
    import backend.main as main
    saved=person(client);before=current(client);thread=before['thread'];sent=[];moves=[]
    monkeypatch.setattr(main,'DEMO',False)
    def forward(account,latest,subject,recipient,note):
        sent.append((account.id,latest.sender,subject,recipient,note))
        return '<forward-test@example.invalid>'
    monkeypatch.setattr(smtp_service,'send_forward',forward)
    monkeypatch.setattr(main.imap_service,'move_thread',lambda db,t,target:moves.append((t.account_id,target)))
    response=action(client,'delegate',body='Please organise this meeting.',delegate_id=saved['id'],archive_after_delegate=archive)
    assert response.status_code==200 and response.json()['delegate_archived']==archive
    assert sent==[(thread['account']['id'],thread['messages'][-1]['sender'],thread['subject'],saved['email'],'Please organise this meeting.')]
    assert moves==([(thread['account']['id'],'archive')] if archive else [])
    with Session() as db:
        assert db.get(Thread,thread['id']).status==('delegated' if archive else 'inbox')
        copy=db.scalar(select(Message).where(Message.message_id=='<forward-test@example.invalid>'))
        assert copy.account_id==thread['account']['id'] and copy.recipient==saved['email'] and copy.folder=='Sent'
        assert 'Please organise this meeting.' in copy.body and thread['messages'][-1]['body'] in copy.body
        assert '---------- Forwarded message ----------' in copy.body
    if archive:
        assert len(client.get('/api/threads?status=delegated').json())==1
        assert client.post(f"/api/threads/{thread['id']}/action",json={'action':'delegate','body':'Again','delegate_id':saved['id'],'session_id':'test'}).status_code==409
        assert len(sent)==1
    else: assert current(client)['counts']['inbox']==before['counts']['inbox']


def test_failed_forward_preserves_inbox_and_never_archives(client,monkeypatch):
    import backend.main as main
    saved=person(client);before=current(client)
    monkeypatch.setattr(main,'DEMO',False)
    def fail(*args): raise RuntimeError('SMTP unavailable')
    monkeypatch.setattr(smtp_service,'send_forward',fail)
    archive=MagicMock();monkeypatch.setattr(main.imap_service,'move_thread',archive)
    response=action(client,'delegate',body='Please handle',delegate_id=saved['id'])
    assert response.status_code==502
    assert current(client)['thread']['id']==before['thread']['id']
    assert len(current(client)['thread']['messages'])==len(before['thread']['messages'])
    archive.assert_not_called()


def test_archive_failure_never_invites_resending_forward(client,monkeypatch):
    import backend.main as main
    saved=person(client);tid=current(client)['thread']['id'];sent=MagicMock(return_value='<sent-forward@example.invalid>')
    monkeypatch.setattr(main,'DEMO',False);monkeypatch.setattr(smtp_service,'send_forward',sent)
    def fail(*args): raise RuntimeError('IMAP unavailable')
    monkeypatch.setattr(main.imap_service,'move_thread',fail)
    result=action(client,'delegate',body='Please handle',delegate_id=saved['id']).json()
    assert not result['delegate_archived'] and 'Do not send it again' in result['warning']
    assert client.post(f'/api/threads/{tid}/action',json={'action':'delegate','body':'Again','delegate_id':saved['id'],'session_id':'test'}).status_code==409
    sent.assert_called_once()


def test_forward_wire_headers_and_message_content(monkeypatch):
    account=SimpleNamespace(username='owner@example.invalid',smtp_port=465,smtp_host='smtp.example.invalid')
    latest=SimpleNamespace(sender='original@example.invalid',sender_name='Original',recipient=account.username,date=1700000000,body='Original text https://example.invalid/paper')
    client=MagicMock();monkeypatch.setattr(smtp_service.smtplib,'SMTP_SSL',lambda *a,**kw:client)
    monkeypatch.setattr(smtp_service,'authenticate',lambda *args:None)
    smtp_service.send_forward(account,latest,'Paper','delegate@example.invalid','Please review')
    message=client.send_message.call_args.args[0]
    assert message['From']==account.username and message['To']=='delegate@example.invalid'
    assert message['Subject']=='Fwd: Paper' and message['In-Reply-To'] is None
    assert message.get_content().startswith('Please review\n\n---------- Forwarded message')
    assert latest.body in message.get_content()
