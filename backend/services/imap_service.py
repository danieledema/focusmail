import imaplib
import email
import re
import time
from sqlalchemy import select
from ..auth import fernet
from ..models import Account, Message, Thread
from .thread_builder import ingest

def connect(account):
    client = imaplib.IMAP4_SSL(account.imap_host, account.imap_port, timeout=20)
    try: client.login(account.username, fernet.decrypt(account.password.encode()).decode())
    except Exception:
        client.logout(); raise
    return client

def select_inbox(client):
    status, _ = client.select('INBOX')
    if status != 'OK': raise RuntimeError('Unable to open INBOX')
    return str((client.response('UIDVALIDITY')[1] or [b''])[0].decode())

def sync_account(db, account):
    client = connect(account)
    try:
        validity = select_inbox(client)
        status, data = client.uid('search', None, 'UNDELETED')
        if status != 'OK': raise RuntimeError('Unable to list messages')
        for uid in data[0].split():
            uid_text = uid.decode()
            if db.scalar(select(Message.id).where(Message.account_id == account.id, Message.uid == uid_text, Message.uidvalidity == validity)):
                continue
            status, fetched = client.uid('fetch', uid, '(BODY.PEEK[])')
            if status != 'OK': raise RuntimeError('Unable to fetch a message')
            for item in fetched:
                if isinstance(item, tuple): ingest(db, account, email.message_from_bytes(item[1]), uid_text, validity); break
        # Remove conversations from the queue when they were moved in another client.
        current_uids = {uid.decode() for uid in data[0].split()}
        for thread in db.scalars(select(Thread).where(Thread.account_id == account.id, Thread.status == 'inbox')):
            known = list(db.scalars(select(Message).where(Message.thread_id == thread.id, Message.folder == 'INBOX', Message.uidvalidity == validity)))
            if known and not any(m.uid in current_uids for m in known):
                thread.status = 'archived'
        # Cache sent-folder history without adding sent-only conversations to the inbox.
        sent_folder = special_folder(client, 'Sent')
        if sent_folder:
            status, _ = client.select(sent_folder, readonly=True)
            if status == 'OK':
                sent_validity = str((client.response('UIDVALIDITY')[1] or [b''])[0].decode())
                status, sent_data = client.uid('search', None, 'ALL')
                if status == 'OK':
                    for uid in sent_data[0].split():
                        sent_uid = 'sent:' + uid.decode()
                        if db.scalar(select(Message.id).where(Message.account_id == account.id, Message.uid == sent_uid, Message.uidvalidity == sent_validity)):
                            continue
                        status, fetched = client.uid('fetch', uid, '(BODY.PEEK[])')
                        if status == 'OK':
                            for item in fetched:
                                if isinstance(item, tuple):
                                    ingest(db, account, email.message_from_bytes(item[1]), sent_uid, sent_validity, folder='Sent')
                                    break
        account.last_sync = time.time(); account.sync_error = '' 
    finally: client.logout()

def special_folder(client, name):
    status, folders = client.list()
    if status == 'OK':
        for folder in folders or []:
            text = folder.decode(errors='replace')
            if re.search(r'\\' + name + r'(?=\s|\))', text, re.I):
                match = re.search(r'\)\s+(?:"[^"]*"|NIL)\s+(.+)$', text)
                if match: return match.group(1)
    return None

def destination(client, action):
    found = special_folder(client, 'Trash' if action == 'delete' else 'Archive')
    if found: return found
    candidates = ['Trash', '[Gmail]/Trash', 'Deleted Items'] if action == 'delete' else ['Archive', '[Gmail]/All Mail', 'Archives']
    for folder in candidates:
        if client.list('""', '"' + folder + '"')[0] == 'OK':
            _, rows = client.list('""', '"' + folder + '"')
            if rows: return '"' + folder + '"'
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
