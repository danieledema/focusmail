import re
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime, parseaddr
from html.parser import HTMLParser
from sqlalchemy import select
from ..models import Message, Thread
import time

class PlainHTML(HTMLParser):
    def __init__(self):
        super().__init__(); self.parts = []; self.hidden = 0
    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style', 'head'): self.hidden += 1
        if tag in ('p', 'div', 'br', 'li') and not self.hidden: self.parts.append('\n')
    def handle_endtag(self, tag):
        if tag in ('script', 'style', 'head'): self.hidden = max(0, self.hidden - 1)
    def handle_data(self, data):
        if not self.hidden: self.parts.append(data)

def header(value):
    try: return str(make_header(decode_header(str(value or ''))))
    except (LookupError, UnicodeError): return str(value or '')

def subject_key(subject):
    return re.sub(r'^(?:(?:re|fw|fwd)\s*:\s*)+', '', subject, flags=re.I).strip().casefold()

def text_body(raw):
    parts = list(raw.walk()) if raw.is_multipart() else [raw]
    candidates = [p for p in parts if p.get_content_type() == 'text/plain' and p.get_content_disposition() != 'attachment']
    if not candidates:
        candidates = [p for p in parts if p.get_content_type() == 'text/html' and p.get_content_disposition() != 'attachment']
    chunks = []
    for p in candidates:
        try: text = (p.get_payload(decode=True) or b'').decode(p.get_content_charset() or 'utf-8', errors='replace')
        except LookupError: text = (p.get_payload(decode=True) or b'').decode('utf-8', errors='replace')
        if p.get_content_type() == 'text/html':
            parser = PlainHTML(); parser.feed(text); text = ''.join(parser.parts)
        chunks.append(text)
    return '\n'.join(chunks).strip()[:100000]

def ingest(db, account, raw, uid, validity, folder='INBOX', create_if_missing=True):
    if db.scalar(select(Message).where(Message.account_id == account.id, Message.uid == uid, Message.uidvalidity == validity)):
        return
    message_id = str(raw.get('Message-ID', ''))
    if message_id:
        existing = db.scalar(select(Message).where(Message.account_id == account.id, Message.message_id == message_id))
        if existing:
            if folder == 'INBOX':
                existing.uid = uid; existing.uidvalidity = validity; existing.folder = folder
            return
    subject = header(raw.get('Subject')) or '(No subject)'
    refs = str(raw.get('References', '')) + ' ' + str(raw.get('In-Reply-To', ''))
    referenced = db.scalar(select(Message).where(Message.account_id == account.id, Message.message_id.in_(re.findall(r'<[^>]+>', refs))))
    thread = db.get(Thread, referenced.thread_id) if referenced else None
    if not thread:
        thread = next((t for t in db.scalars(select(Thread).where(Thread.account_id == account.id)) if subject_key(t.subject) == subject_key(subject)), None)
    if not thread and not create_if_missing:
        return
    try: date = parsedate_to_datetime(raw.get('Date')).timestamp()
    except (TypeError, ValueError, OverflowError): date = time.time()
    if not thread:
        thread = Thread(account_id=account.id, subject=subject, oldest=date, status='inbox' if folder == 'INBOX' else 'replied'); db.add(thread); db.flush()
    elif folder == 'INBOX' and thread.status in ('archived', 'replied', 'deleted'):
        thread.status = 'inbox'
    thread.oldest = min(thread.oldest, date)
    name, sender = parseaddr(header(raw.get('From')))
    db.add(Message(thread_id=thread.id, account_id=account.id, uid=uid, uidvalidity=validity, folder=folder,
                   message_id=str(raw.get('Message-ID', '')), references=refs.strip(), sender=sender,
                   sender_name=name or sender.split('@')[0], recipient=header(raw.get('To')), date=date, body=text_body(raw)))
    db.flush()
