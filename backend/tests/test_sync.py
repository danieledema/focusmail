import email
import threading
import time
from sqlalchemy import select, func
from backend.database import Session
from backend.models import Account, Message


def test_sync_saves_inbox_before_sent_download_and_survives_failure(client, monkeypatch):
    from backend.services import imap_service
    from backend.main import mutation_lock
    class Mailbox:
        folder = 'INBOX'
        def select(self, folder, **kwargs):
            self.folder = folder.strip('"')
            return 'OK', [b'2']
        def response(self, *args): return 'OK', [b'fresh-validity']
        def list(self): return 'OK', [b'(\\Sent) "/" "Sent"']
        def uid(self, command, *args):
            if command == 'search': return 'OK', [b'901 902' if self.folder == 'INBOX' else b'903']
            if self.folder == 'Sent':
                # All inbox mail must already be committed and the write lock released.
                from concurrent.futures import ThreadPoolExecutor
                def available():
                    acquired = mutation_lock.acquire(blocking=False)
                    if acquired: mutation_lock.release()
                    return acquired
                with ThreadPoolExecutor() as pool:
                    assert pool.submit(available).result(timeout=1)
                with Session() as observer:
                    assert observer.scalar(select(func.count()).select_from(Message).where(Message.uidvalidity == 'fresh-validity')) == 2
                raise TimeoutError('Sent history unavailable')
            raw = email.message_from_string(f'Subject: Sync regression {args[0].decode()}\nMessage-ID: <{args[0].decode()}@sync.example>\nFrom: sender@example.com\n\nInbox body')
            return 'OK', [(b'body', raw.as_bytes())]
        def logout(self): pass
    monkeypatch.setattr(imap_service, 'connect', lambda account: Mailbox())
    updates = []
    import pytest
    with Session() as db:
        with pytest.raises(TimeoutError):
            imap_service.sync_account(db, db.scalar(select(Account)), mutation_lock, lambda **v: updates.append(v))
    assert any(v['folder'] == 'INBOX' and v['completed'] == 2 for v in updates)


def test_explicit_sync_runs_once_and_cached_ui_reads_stay_responsive(client, monkeypatch):
    import backend.main as main
    entered = threading.Event()
    release = threading.Event()
    calls = []
    def slow_mail(*args):
        calls.append(1)
        entered.set()
        release.wait(3)
    monkeypatch.setattr(main, 'DEMO', False)
    monkeypatch.setattr(main.imap_service, 'sync_account', slow_mail)
    try:
        assert client.post('/api/sync').status_code == 200
        assert entered.wait(1)
        assert client.get('/api/sync').json()['running']
        assert client.get('/api/state').json()['sync']['running']
        assert client.post('/api/sync').status_code == 200
        assert len(calls) == 1
    finally:
        release.set()
        deadline = time.monotonic() + 3
        while main.sync_snapshot()['running'] and time.monotonic() < deadline:
            time.sleep(.01)
    assert not main.sync_snapshot()['running']


def test_sent_window_filters_before_body_fetch_and_prunes_only_local_sent(client, monkeypatch):
    from backend.services import imap_service
    from backend.models import Thread
    from datetime import datetime, timezone
    from email.utils import format_datetime
    now = time.time()
    recent = now - 3600
    old = now - 25 * 3600
    calls = []
    class Mailbox:
        folder = 'INBOX'
        def select(self, folder, **kwargs):
            self.folder = folder.strip('"')
            return 'OK', [b'2']
        def response(self, *args): return 'OK', [b'window-validity']
        def list(self): return 'OK', [b'(\\Sent) "/" "Sent"']
        def uid(self, command, *args):
            calls.append((self.folder, command, *args))
            if command == 'search': return 'OK', [b'11' if self.folder == 'INBOX' else b'21 22']
            uid = args[0]
            stamp = recent if uid == b'22' else old
            header = f'Date: {format_datetime(datetime.fromtimestamp(stamp, timezone.utc))}\n'
            if 'HEADER.FIELDS' in args[1]:
                return 'OK', [(b'date header', header.encode())]
            body = header + f'Subject: Window {uid.decode()}\nMessage-ID: <window-{uid.decode()}@example.com>\nFrom: sender@example.com\n\nBody'
            return 'OK', [(b'message', body.encode())]
        def logout(self): pass
    monkeypatch.setattr(imap_service, 'connect', lambda account: Mailbox())
    with Session() as db:
        account = db.scalar(select(Account))
        sent_only = Thread(account_id=account.id, subject='Old sent-only', oldest=old, status='replied')
        mixed = Thread(account_id=account.id, subject='Keep inbox context', oldest=old, status='inbox')
        db.add_all([sent_only, mixed]); db.flush()
        sent_only_id, mixed_id = sent_only.id, mixed.id
        db.add_all([
            Message(account_id=account.id, thread_id=sent_only.id, uid='sent:old1', folder='Sent', date=old),
            Message(account_id=account.id, thread_id=mixed.id, uid='sent:old2', folder='Sent', date=old),
            Message(account_id=account.id, thread_id=mixed.id, uid='keep-context', uidvalidity='other', folder='INBOX', date=old),
        ])
        db.commit()
        imap_service.sync_account(db, account)
    with Session() as db:
        assert db.get(Thread, sent_only_id) is None
        assert db.get(Thread, mixed_id) is not None
        assert db.scalar(select(Message.id).where(Message.uid == 'keep-context'))
        assert db.scalar(select(Message.id).where(Message.uid == '11'))  # Inbox has no age limit.
        assert db.scalar(select(Message.id).where(Message.uid == 'sent:22'))
        assert not db.scalar(select(Message.id).where(Message.uid == 'sent:21'))
        assert not db.scalar(select(Message.id).where(Message.folder == 'Sent', Message.date < now - 86400))
    assert ('INBOX', 'search', None, 'UNDELETED') in calls
    assert any(c[:2] == ('Sent', 'search') and 'SENTSINCE' in c for c in calls)
    assert not any(c[:3] == ('Sent', 'fetch', b'21') and c[3] == '(BODY.PEEK[])' for c in calls)
    assert all(c[1] in ('search', 'fetch') for c in calls)  # No server-side deletions.


def test_sent_date_timezone_and_missing_header_fallback():
    from backend.services.imap_service import sent_timestamp
    from datetime import datetime, timezone
    assert sent_timestamp(b'Date: Fri, 16 Oct 2026 15:30:00 +0200\r\n', b'') == datetime(2026,10,16,13,30,tzinfo=timezone.utc).timestamp()
    assert sent_timestamp(b'Date: invalid\r\n', b'1 (INTERNALDATE "16-Oct-2026 13:30:00 +0000")') == datetime(2026,10,16,13,30,tzinfo=timezone.utc).timestamp()
    assert sent_timestamp(b'', b'') is None
