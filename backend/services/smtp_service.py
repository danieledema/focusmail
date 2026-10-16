import smtplib
import ssl
from email.message import EmailMessage
from email.utils import make_msgid, formatdate
from ..auth import fernet

def send_reply(account, latest, subject, body):
    message = EmailMessage()
    message['From'] = account.username
    message['To'] = latest.sender
    message['Subject'] = subject if subject.lower().startswith('re:') else 'Re: ' + subject
    message['Message-ID'] = make_msgid()
    message['Date'] = formatdate(localtime=True)
    if latest.message_id:
        message['In-Reply-To'] = latest.message_id
        message['References'] = (latest.references + ' ' + latest.message_id).strip()[-4000:]
    message.set_content(body)
    if account.smtp_port == 465:
        client = smtplib.SMTP_SSL(account.smtp_host, account.smtp_port, timeout=20, context=ssl.create_default_context())
    else:
        client = smtplib.SMTP(account.smtp_host, account.smtp_port, timeout=20)
    try:
        if account.smtp_port != 465:
            client.ehlo(); client.starttls(context=ssl.create_default_context()); client.ehlo()
        client.login(account.username, fernet.decrypt(account.password.encode()).decode())
        client.send_message(message)
    finally: client.quit()
    return message['Message-ID']

def test_smtp(account):
    if account.smtp_port == 465:
        client = smtplib.SMTP_SSL(account.smtp_host, account.smtp_port, timeout=20, context=ssl.create_default_context())
    else:
        client = smtplib.SMTP(account.smtp_host, account.smtp_port, timeout=20)
    try:
        if account.smtp_port != 465: client.starttls(context=ssl.create_default_context())
        client.login(account.username, fernet.decrypt(account.password.encode()).decode())
    finally: client.quit()
