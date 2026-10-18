import smtplib
import ssl
from email.message import EmailMessage
from email.utils import make_msgid, formatdate
from ..auth import fernet
from .oauth_service import access_token

def authenticate(client, account):
    if account.auth_type == 'oauth2':
        token = access_token(account)
        client.auth('XOAUTH2', lambda challenge=None: f'user={account.username}\x01auth=Bearer {token}\x01\x01' if challenge is None else '', initial_response_ok=True)
    else:
        client.login(account.username, fernet.decrypt(account.password.encode()).decode())

def send_reply(account, latest, subject, body):
    message = EmailMessage()
    message['From'] = account.username
    message['To'] = latest.sender
    message['Subject'] = subject if subject.lower().startswith('re:') else 'Re: ' + subject
    message['Message-ID'] = make_msgid()
    message['Date'] = formatdate(localtime=True)
    if latest.message_id:
        message['In-Reply-To'] = ' '.join(latest.message_id.split())
        message['References'] = ' '.join((latest.references + ' ' + latest.message_id).split())[-4000:]
    message.set_content(body)
    return send_message(account,message)

def forward_body(latest, subject, note):
    return (note.strip()+'\n\n---------- Forwarded message ----------\n'
            +f'From: {latest.sender_name} <{latest.sender}>\n'
            +f'Date: {formatdate(latest.date,localtime=True)}\n'
            +f'Subject: {subject}\nTo: {latest.recipient}\n\n'+latest.body)

def send_forward(account, latest, subject, recipient, note):
    message=EmailMessage()
    message['From']=account.username
    message['To']=recipient
    message['Subject']=subject if subject.lower().startswith(('fwd:','fw:')) else 'Fwd: '+subject
    message['Message-ID']=make_msgid()
    message['Date']=formatdate(localtime=True)
    message.set_content(forward_body(latest,subject,note))
    return send_message(account,message)

def send_message(account, message):
    if account.smtp_port == 465:
        client = smtplib.SMTP_SSL(account.smtp_host, account.smtp_port, timeout=20, context=ssl.create_default_context())
    else:
        client = smtplib.SMTP(account.smtp_host, account.smtp_port, timeout=20)
    try:
        if account.smtp_port != 465:
            client.ehlo(); client.starttls(context=ssl.create_default_context()); client.ehlo()
        authenticate(client, account)
        client.send_message(message)
    finally:
        try: client.quit()
        except Exception: client.close()
    return message['Message-ID']

def test_smtp(account):
    if account.smtp_port == 465:
        client = smtplib.SMTP_SSL(account.smtp_host, account.smtp_port, timeout=20, context=ssl.create_default_context())
    else:
        client = smtplib.SMTP(account.smtp_host, account.smtp_port, timeout=20)
    try:
        if account.smtp_port != 465: client.starttls(context=ssl.create_default_context())
        authenticate(client, account)
    finally:
        try: client.quit()
        except Exception: client.close()
