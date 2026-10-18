"""Safe connection diagnostics: classify provider responses without echoing secrets."""
import imaplib
import smtplib
import socket
import ssl

GOOGLE_HELP = 'https://support.google.com/accounts/answer/185833'


def connection_error(error, service='IMAP'):
    raw = str(error).lower()  # For classification only; never return/log raw provider text.
    code = 'connection_failed'
    message = 'Could not connect. Check the server, port, and network connection.'
    help_url = None
    from .oauth_service import OAuthError
    if isinstance(error, OAuthError):
        return {'service':service, 'status':'error', 'code':'oauth_reconnect_required', 'message':f'{service}: {error}', 'help_url':None}
    if 'application-specific password required' in raw or 'app password required' in raw:
        code = 'app_password_required'
        message = 'Google requires an app password for this connection. Generate one in your Google Account (2-Step Verification must be enabled), then update the saved password here.'
        help_url = GOOGLE_HELP
    elif isinstance(error, ssl.SSLCertVerificationError):
        code = 'certificate_verification_failed'
        message = 'The server’s TLS certificate could not be verified. Check the hostname and your computer’s date; certificate verification remains enabled.'
    elif isinstance(error, socket.gaierror):
        code = 'hostname_not_found'
        message = 'The server hostname could not be resolved. Check the hostname and your internet connection; enter only the hostname, without https://.'
    elif isinstance(error, (TimeoutError, socket.timeout)):
        code = 'connection_timeout'
        message = 'The server did not respond in time. Check the hostname, port, and whether your network blocks mail connections.'
    elif isinstance(error, smtplib.SMTPNotSupportedError):
        code = 'tls_not_supported'
        message = 'This SMTP server did not offer the required secure connection. Use your provider’s TLS/STARTTLS host and port.'
    elif isinstance(error, (imaplib.IMAP4.error, smtplib.SMTPAuthenticationError)):
        if 'authenticationfailed' in raw or 'authentication failed' in raw or 'invalid credentials' in raw or 'login' in raw or isinstance(error, smtplib.SMTPAuthenticationError):
            code = 'authentication_failed'
            message = 'The server rejected the login. Check the full email username and app password. Your provider may require an app password or OAuth sign-in.'
        else:
            code = 'provider_rejected_connection'
            message = 'The mail provider rejected the connection. Check whether IMAP/SMTP password access is enabled for this account.'
    elif isinstance(error, ConnectionRefusedError):
        code = 'connection_refused'
        message = 'The server refused the connection. Check the hostname and port.'
    elif isinstance(error, ssl.SSLError):
        code = 'tls_handshake_failed'
        message = 'The secure connection failed. Check the port: IMAP requires implicit TLS; SMTP uses TLS on 465 and STARTTLS on other ports.'
    return {'service':service, 'status':'error', 'code':code, 'message':f'{service}: {message}', 'help_url':help_url}
