import imaplib
import ssl
import email
import re
import time
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from contextlib import nullcontext
from sqlalchemy import select
from ..auth import fernet
from ..models import Account, Message, Thread
from .thread_builder import ingest
from .oauth_service import access_token

def connect(account):
    client = imaplib.IMAP4_SSL(account.imap_host, account.imap_port, timeout=20, ssl_context=ssl.create_default_context())
    try:
        if account.auth_type == 'oauth2':
            token = access_token(account)
            client.authenticate('XOAUTH2', lambda challenge: f'user={account.username}\x01auth=Bearer {token}\x01\x01'.encode() if not challenge else b'')
        else:
            client.login(account.username, fernet.decrypt(account.password.encode()).decode())
        # imaplib caches the greeting's capabilities. Providers such as Gmail
        # advertise MOVE/UIDPLUS only after authentication, so refresh explicitly.
        status, capabilities = client.capability()
        if status != 'OK' or not capabilities:
            raise RuntimeError('Unable to confirm authenticated IMAP capabilities')
        client.capabilities = tuple(
            token.upper() for row in capabilities if isinstance(row, bytes)
            for token in row.split()
        )
    except Exception:
        try: client.logout()
        except Exception: pass
        raise
    return client

def select_inbox(client):
    status, _ = client.select('INBOX')
    if status != 'OK': raise RuntimeError('Unable to open INBOX')
    return str((client.response('UIDVALIDITY')[1] or [b''])[0].decode())

SENT_RETENTION_SECONDS = 24 * 60 * 60

def sent_timestamp(headers, metadata):
    try:
        date = parsedate_to_datetime(email.message_from_bytes(headers).get('Date'))
        return date.replace(tzinfo=timezone.utc).timestamp() if date.tzinfo is None else date.timestamp()
    except (TypeError, ValueError, OverflowError):
        internal = imaplib.Internaldate2tuple(metadata)
        return time.mktime(internal) if internal else None

def prune_sent_cache(db, account_id, cutoff):
    stale = list(db.scalars(select(Message).where(
        Message.account_id == account_id, Message.folder == 'Sent', Message.date < cutoff)))
    thread_ids = {message.thread_id for message in stale}
    for message in stale:
        db.delete(message)
    db.flush()
    for thread_id in thread_ids:
        if not db.scalar(select(Message.id).where(Message.thread_id == thread_id).limit(1)):
            thread = db.get(Thread, thread_id)
            if thread:
                db.delete(thread)
    db.commit()

def sync_account(db, account, mutation_lock=None, progress=None):
    """Download on request, keeping network waits outside the local write lock."""
    guard = mutation_lock or nullcontext()
    report = progress or (lambda **values: None)
    cutoff = time.time() - SENT_RETENTION_SECONDS
    with guard:
        prune_sent_cache(db, account.id, cutoff)
    client = connect(account)
    try:
        for folder in ('INBOX', 'Sent'):
            report(folder=folder, completed=0, total=0)
            if folder == 'INBOX':
                validity = select_inbox(client)
            else:
                sent = special_folder(client, 'Sent')
                if not sent:
                    continue
                if client.select(sent, readonly=True)[0] != 'OK':
                    raise RuntimeError('Unable to open Sent folder')
                validity = str((client.response('UIDVALIDITY')[1] or [b''])[0].decode())
            criteria = ['UNDELETED']
            if folder == 'Sent':
                # IMAP dates have day precision and omit timezone: use a broad
                # server filter, then check the exact cutoff before fetching bodies.
                day = datetime.fromtimestamp(cutoff, timezone.utc) - timedelta(days=1)
                months = ('Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec')
                criteria.extend(['SENTSINCE', f'{day.day:02d}-{months[day.month-1]}-{day.year}'])
            status, data = client.uid('search', None, *criteria)
            if status != 'OK':
                raise RuntimeError('Unable to list messages')
            uids = (data[0] or b'').split()
            prefix = '' if folder == 'INBOX' else 'sent:'
            with guard:
                known = set(db.scalars(select(Message.uid).where(
                    Message.account_id == account.id, Message.uidvalidity == validity,
                    Message.folder == folder, Message.body_version >= 1)))
                db.commit()  # No read snapshot or write transaction across network I/O.
            report(folder=folder, completed=0, total=len(uids))
            # Newest messages become available first on the initial import.
            for completed, uid in enumerate(reversed(uids), 1):
                uid_text = prefix + uid.decode()
                if uid_text not in known:
                    if folder == 'Sent':
                        status, headers = client.uid('fetch', uid, '(INTERNALDATE BODY.PEEK[HEADER.FIELDS (DATE)])')
                        if status != 'OK':
                            raise RuntimeError('Unable to check a Sent message date')
                        header = next((item for item in headers if isinstance(item, tuple)), None)
                        if header is None:
                            raise RuntimeError('The mail server returned an incomplete Sent date')
                        sent_at = sent_timestamp(header[1], header[0])
                        if sent_at is None or sent_at < cutoff:
                            report(folder=folder, completed=completed, total=len(uids))
                            continue
                    status, fetched = client.uid('fetch', uid, '(BODY.PEEK[])')
                    if status != 'OK':
                        raise RuntimeError('Unable to fetch a message')
                    raw = next((item[1] for item in fetched if isinstance(item, tuple)), None)
                    if raw is None:
                        raise RuntimeError('The mail server returned an incomplete message')
                    with guard:
                        ingest(db, account, email.message_from_bytes(raw), uid_text, validity, folder=folder)
                        db.commit()  # Keep inbox mail even if a later Sent download fails.
                report(folder=folder, completed=completed, total=len(uids))
            if folder == 'INBOX':
                current_uids = {uid.decode() for uid in uids}
                with guard:
                    for thread in db.scalars(select(Thread).where(Thread.account_id == account.id, Thread.status == 'inbox')):
                        messages = list(db.scalars(select(Message).where(Message.thread_id == thread.id, Message.folder == 'INBOX', Message.uidvalidity == validity)))
                        if messages and not any(m.uid in current_uids for m in messages):
                            thread.status = 'archived'
                    db.commit()
        with guard:
            prune_sent_cache(db, account.id, time.time() - SENT_RETENTION_SECONDS)
            account.last_sync = time.time(); account.sync_error = ''
            db.commit()
    finally:
        try: client.logout()
        except Exception: client.shutdown()

def special_folder(client, name):
    status, folders = client.list()
    if status == 'OK':
        for folder in folders or []:
            if not isinstance(folder, bytes): continue
            text = folder.decode(errors='replace')
            if re.search(r'\\' + name + r'(?=\s|\))', text, re.I):
                match = re.search(r'\)\s+(?:"[^"]*"|NIL)\s+(.+)$', text)
                if match: return match.group(1)
    return None

def destination(client, action):
    gmail_archive = action == 'archive' and b'X-GM-EXT-1' in getattr(client, 'capabilities', ())
    found = special_folder(client, 'Trash' if action == 'delete' else 'All' if gmail_archive else 'Archive')
    if found: return found
    candidates = ['Trash', '[Gmail]/Trash', 'Deleted Items'] if action == 'delete' else ['Archive', '[Gmail]/All Mail', 'Archives']
    for folder in candidates:
        if client.list('""', '"' + folder + '"')[0] == 'OK':
            _, rows = client.list('""', '"' + folder + '"')
            if any(isinstance(row, bytes) and row for row in rows or []): return '"' + folder + '"'
    raise RuntimeError('No provider folder found. Create an Archive/Trash folder and try again.')

def move_thread(db, thread, action):
    account = db.get(Account, thread.account_id)
    client = connect(account)
    try:
        validity = select_inbox(client)
        target = destination(client, action)
        messages = list(db.scalars(select(Message).where(Message.thread_id == thread.id, Message.uidvalidity == validity, Message.folder == 'INBOX')))
        ids = ','.join(m.uid for m in messages if m.uid and m.uid.isdigit())
        if not ids:
            raise RuntimeError('Mailbox identifiers changed. Sync your account before trying again.')
        if client.uid('store', ids, '+FLAGS.SILENT', '(\\Seen)')[0] != 'OK': raise RuntimeError('Unable to mark messages read')
        if b'MOVE' in client.capabilities or 'MOVE' in client.capabilities:
            if client.uid('MOVE', ids, target)[0] != 'OK': raise RuntimeError('Provider refused to move thread')
        elif b'UIDPLUS' in client.capabilities or 'UIDPLUS' in client.capabilities:
            if client.uid('COPY', ids, target)[0] != 'OK': raise RuntimeError('Provider refused to copy thread')
            if client.uid('store', ids, '+FLAGS.SILENT', '(\\Deleted)')[0] != 'OK': raise RuntimeError('Provider refused to remove original')
            if client.uid('EXPUNGE', ids)[0] != 'OK': raise RuntimeError('Provider refused to finish move')
        else:
            raise RuntimeError('This server needs IMAP MOVE or UIDPLUS for safe thread moves')
    finally: client.logout()
