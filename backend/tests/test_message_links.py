from email import message_from_string
from sqlalchemy import select
from backend.services.thread_builder import text_body, ingest
from backend.database import Session
from backend.models import Account, Message, Thread


def html_email(body):
    return message_from_string('Content-Type: text/html; charset=utf-8\nSubject: Link regression\nMessage-ID: <link-regression@example.com>\n\n'+body)


def test_html_links_preserve_labels_and_destinations_without_active_html():
    raw=html_email('<head><style>hidden</style></head><p>Read <a href="https://example.com/paper?a=1&amp;b=2"><strong>The paper</strong></a>.</p><a href="mailto:author@example.com">Contact author</a><script>steal()</script><img src="https://tracking.example/pixel">')
    body=text_body(raw)
    assert '[The paper](https://example.com/paper?a=1&b=2)' in body
    assert '[Contact author](mailto:author@example.com)' in body
    assert all(value not in body for value in ('hidden','steal()','tracking.example','<script','<strong'))


def test_unsafe_links_stay_text_and_url_parentheses_are_escaped():
    body=text_body(html_email('<a href="javascript:alert(1)">Unsafe</a><a href="data:text/html,evil">Data</a><a href="https://user:secret@example.com">Credentials</a><a href="https://example.com/a(b)">Paper</a>'))
    assert body.startswith('UnsafeDataCredentials')
    assert 'javascript:' not in body and 'data:' not in body and 'secret' not in body
    assert '[Paper](https://example.com/a%28b%29)' in body


def test_reimport_upgrades_old_cached_body_without_duplicate_or_thread_state_change(client):
    raw=html_email('<a href="https://example.com/paper">Paper title</a>')
    with Session() as db:
        account=db.scalar(select(Account))
        thread=Thread(account_id=account.id,subject='Link regression',oldest=1,status='snoozed')
        db.add(thread);db.flush()
        message=Message(account_id=account.id,thread_id=thread.id,uid='links1',uidvalidity='1',folder='INBOX',message_id='<link-regression@example.com>',body='Paper title',body_version=0)
        db.add(message);db.commit()
        ingest(db,account,raw,'links1','1');db.commit()
        assert message.body=='[Paper title](https://example.com/paper)'
        assert message.body_version==1 and thread.status=='snoozed'
        assert len(list(db.scalars(select(Message).where(Message.uid=='links1'))))==1
