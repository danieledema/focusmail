import re
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime, parseaddr
from html.parser import HTMLParser
from urllib.parse import urlsplit
from sqlalchemy import select
from ..models import Message, Thread
import time

def safe_link(value):
    value = (value or '').strip()
    if not value or re.search(r'[\s\x00-\x1f\x7f]', value): return None
    try:
        url = urlsplit(value)
        if url.scheme.lower() in ('http', 'https') and url.hostname and not url.username and not url.password:
            return value.replace('(', '%28').replace(')', '%29')
        if url.scheme.lower() == 'mailto' and url.path:
            return value.replace('(', '%28').replace(')', '%29')
    except ValueError: pass
    return None

class PlainHTML(HTMLParser):
    def __init__(self):
        super().__init__(); self.parts = []; self.hidden = 0; self.anchor = None
    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style', 'head'): self.hidden += 1
        if self.hidden: return
        if tag in ('p', 'div', 'br', 'li'): self.parts.append('\n')
        if tag == 'a':
            self.finish_anchor()
            href = safe_link(dict(attrs).get('href'))
            if href: self.anchor = (len(self.parts), href)
    def finish_anchor(self):
        if self.anchor:
            start, href = self.anchor
            label = ' '.join(''.join(self.parts[start:]).split())
            label = label.replace('[', '(').replace(']', ')') or href
            self.parts[start:] = [f'[{label}]({href})']
            self.anchor = None
    def handle_endtag(self, tag):
        if tag in ('script', 'style', 'head'): self.hidden = max(0, self.hidden - 1)
        if tag == 'a' and not self.hidden: self.finish_anchor()
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
            parser = PlainHTML(); parser.feed(text); parser.close(); parser.finish_anchor(); text = ''.join(parser.parts)
        chunks.append(text)
    return '\n'.join(chunks).strip()[:100000]

def ingest(db, account, raw, uid, validity, folder='INBOX', create_if_missing=True):
    existing = db.scalar(select(Message).where(Message.account_id == account.id, Message.uid == uid, Message.uidvalidity == validity))
    if existing:
        if existing.body_version < 1:
            existing.body = text_body(raw); existing.body_version = 1
        return
    message_id = str(raw.get('Message-ID', ''))
    if message_id:
        existing = db.scalar(select(Message).where(Message.account_id == account.id, Message.message_id == message_id))
        if existing:
            if existing.body_version < 1:
                existing.body = text_body(raw); existing.body_version = 1
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
    elif folder == 'INBOX' and thread.status in ('archived', 'replied', 'delegated', 'deleted'):
        thread.status = 'inbox'
    thread.oldest = min(thread.oldest, date)
    name, sender = parseaddr(header(raw.get('From')))
    db.add(Message(thread_id=thread.id, account_id=account.id, uid=uid, uidvalidity=validity, folder=folder,
                   message_id=str(raw.get('Message-ID', '')), references=refs.strip(), sender=sender,
                   sender_name=name or sender.split('@')[0], recipient=header(raw.get('To')), date=date, body=text_body(raw)))
    db.flush()
