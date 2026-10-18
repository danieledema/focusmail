"""Desktop/public-client OAuth: PKCE, verified OIDC identity, private loopback callback.
Tokens stay in Python and encrypted SQLite. No periodic refresh or mail fetching.
"""
import base64
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import logging
import re
import secrets
import threading
import time
from urllib.parse import urlencode, urlsplit, parse_qs
import uuid

import httpx
import jwt
from sqlalchemy import select
from sqlalchemy.orm.attributes import set_committed_value
from ..auth import fernet
from ..database import Session
from ..models import Account, Setting

log = logging.getLogger('focusmail.oauth')
PROVIDERS = {
    'google': {
        'name':'Google', 'authorize':'https://accounts.google.com/o/oauth2/v2/auth',
        'token':'https://oauth2.googleapis.com/token', 'jwks':'https://www.googleapis.com/oauth2/v3/certs',
        'scope':'openid email https://mail.google.com/', 'imap':'imap.gmail.com', 'smtp':'smtp.gmail.com',
        'help_url':'https://developers.google.com/identity/protocols/oauth2/native-app',
    },
    'microsoft': {
        'name':'Microsoft', 'authorize':'https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize',
        'token':'https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token',
        'jwks':'https://login.microsoftonline.com/common/discovery/v2.0/keys',
        'scope':'openid profile email offline_access https://outlook.office.com/IMAP.AccessAsUser.All https://outlook.office.com/SMTP.Send',
        'imap':'outlook.office365.com', 'smtp':'smtp.office365.com',
        'help_url':'https://learn.microsoft.com/en-us/exchange/client-developer/legacy-protocols/how-to-authenticate-an-imap-pop-smtp-application-by-using-oauth',
    },
}
CONSUMER_TENANT = '9188040d-6c67-4c5b-b112-36a304b66dad'
ATTEMPT_TTL = 600
_attempts = {}
_attempt_lock = threading.RLock()
_refresh_lock = threading.RLock()

class OAuthError(Exception):
    """A safe user-facing error; contains no provider response, code, or token."""


def configuration(db, provider):
    if provider not in PROVIDERS: raise OAuthError('Unknown sign-in provider.')
    def read(key, default=''):
        row = db.get(Setting, f'oauth.{provider}.{key}')
        return row.value if row else default
    encrypted = db.get(Setting, f'secret.oauth.{provider}.client_secret')
    return {'client_id':read('client_id'), 'tenant':read('tenant','common'),
            'client_secret':fernet.decrypt(encrypted.value.encode()).decode() if encrypted else ''}


def public_providers(db):
    result = []
    for provider, details in PROVIDERS.items():
        config = configuration(db, provider)
        result.append({'id':provider, 'name':details['name'], 'configured':bool(config['client_id']),
                       'client_id':config['client_id'], 'tenant':config['tenant'],
                       'has_client_secret':bool(config['client_secret']), 'help_url':details['help_url']})
    return result


def save_configuration(db, provider, data):
    if provider not in PROVIDERS: raise OAuthError('Unknown sign-in provider.')
    client_id = data.client_id.strip()
    if client_id:
        if provider == 'google' and not client_id.endswith('.apps.googleusercontent.com'):
            raise OAuthError('Use a Google OAuth client ID for a Desktop app (ending in .apps.googleusercontent.com).')
        if provider == 'microsoft':
            try: uuid.UUID(client_id)
            except ValueError: raise OAuthError('Use the Microsoft Application (client) ID, which is a UUID.')
    db.merge(Setting(key=f'oauth.{provider}.client_id', value=client_id))
    db.merge(Setting(key=f'oauth.{provider}.tenant', value=data.tenant.lower()))
    if provider == 'google' and data.client_secret is not None:
        key = f'secret.oauth.{provider}.client_secret'
        if data.client_secret:
            db.merge(Setting(key=key, value=fernet.encrypt(data.client_secret.encode()).decode()))
        else:
            row = db.get(Setting, key)
            if row: db.delete(row)
    db.commit()


def pkce_challenge(verifier):
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()


def _token_request(provider, config, payload):
    data = {'client_id':config['client_id'], **payload}
    if provider == 'google' and config['client_secret']:
        data['client_secret'] = config['client_secret']
    endpoint = PROVIDERS[provider]['token'].format(tenant=config['tenant'])
    try:
        response = httpx.post(endpoint, data=data, timeout=25)
        result = response.json()
    except Exception:
        raise OAuthError('Could not reach the sign-in provider. Check your connection and try again.') from None
    if response.status_code != 200 or not isinstance(result, dict) or result.get('error'):
        code = result.get('error','') if isinstance(result, dict) else ''
        messages = {
            'invalid_grant':'Your sign-in has expired or was revoked. Reconnect this account.',
            'invalid_client':'The provider rejected the OAuth application setup. Check the registered client ID and Google client secret.',
            'unauthorized_client':'This application registration is not allowed to use desktop sign-in. Check the provider setup.',
            'invalid_scope':'The application is missing the requested mail permissions. Check its provider registration.',
            'access_denied':'Sign-in was denied by the account or organization. Check with your account administrator.',
        }
        raise OAuthError(messages.get(code,'The provider could not complete sign-in. Check the application registration and try again.'))
    return result


def _verify_identity(provider, config, token, nonce):
    """Verify signature, audience, issuer, lifetime and per-attempt nonce. No unverified identity is stored."""
    try:
        if provider == 'google':
            issuers = ['https://accounts.google.com','accounts.google.com']
        else:
            unverified = jwt.decode(token, options={'verify_signature':False})
            tenant = str(uuid.UUID(unverified['tid']))
            configured = config['tenant']
            if configured == 'consumers' and tenant != CONSUMER_TENANT: raise ValueError()
            if configured == 'organizations' and tenant == CONSUMER_TENANT: raise ValueError()
            if re.fullmatch(r'[0-9a-f-]{36}', configured) and tenant != configured: raise ValueError()
            issuers = [f'https://login.microsoftonline.com/{tenant}/v2.0']
        signing = jwt.PyJWKClient(PROVIDERS[provider]['jwks'], timeout=15).get_signing_key_from_jwt(token)
        claims = jwt.decode(token, signing.key, algorithms=['RS256'], audience=config['client_id'], issuer=issuers,
                            leeway=30, options={'require':['exp','iat','sub','iss','aud','nonce']})
        if not hmac.compare_digest(str(claims['nonce']).encode(), nonce.encode()): raise ValueError()
        if provider == 'google' and claims.get('email_verified') is not True: raise ValueError()
        address = claims.get('email') if provider == 'google' else claims.get('preferred_username') or claims.get('email')
        if not isinstance(address,str) or not re.fullmatch(r'[^\s@<>\r\n]+@[^\s@<>\r\n]+', address): raise ValueError()
        return {'subject':claims['sub'], 'email':address, 'name':str(claims.get('name') or address)[:100], 'consumer':provider == 'microsoft' and claims.get('tid') == CONSUMER_TENANT}
    except Exception:
        raise OAuthError('Could not verify the signed account identity. No account was connected; please try signing in again.') from None


def _credentials(provider, result, previous=None):
    previous = previous or {}
    access = result.get('access_token')
    refresh = result.get('refresh_token') or previous.get('refresh_token')
    if not isinstance(access,str) or not access or not isinstance(refresh,str) or not refresh:
        raise OAuthError('The provider did not grant persistent sign-in. Reconnect and allow offline access to your mail.')
    scopes = result.get('scope') or previous.get('scope') or PROVIDERS[provider]['scope']
    granted = set(scopes.split())
    if provider == 'google':
        complete = 'https://mail.google.com/' in granted
    else:
        complete = {'imap.accessasuser.all','smtp.send'} <= {scope.rsplit('/',1)[-1].lower() for scope in granted}
    if not complete: raise OAuthError('Mail read/send permission was not granted. Reconnect and approve both mail permissions.')
    try: expires = int(result.get('expires_in',3600))
    except (ValueError,TypeError): raise OAuthError('The provider returned an invalid token lifetime.')
    if expires <= 0: raise OAuthError('The provider returned an expired sign-in token. Please reconnect.')
    return {'access_token':access, 'refresh_token':refresh, 'expires_at':time.time()+expires, 'scope':scopes}


def _pack(credentials): return fernet.encrypt(json.dumps(credentials).encode()).decode()
def _unpack(account):
    try: return json.loads(fernet.decrypt(account.oauth_credentials.encode()))
    except Exception: raise OAuthError('Saved sign-in could not be read. Reconnect this account.') from None


def access_token(account):
    """Called only by a user-requested IMAP/SMTP action. Persist refresh rotation before mail I/O."""
    stored = _unpack(account)
    if stored.get('expires_at',0) > time.time()+60: return stored['access_token']
    with _refresh_lock, Session() as db:
        current = db.get(Account, account.id)
        if not current or current.auth_type != 'oauth2': raise OAuthError('This account is no longer connected.')
        stored = _unpack(current)
        if stored.get('expires_at',0) <= time.time()+60:
            config = configuration(db, current.provider)
            if not config['client_id'] or config['client_id'] != current.oauth_client_id:
                raise OAuthError('The OAuth application setup changed. Reconnect this account with the current setup.')
            result = _token_request(current.provider, config, {'grant_type':'refresh_token', 'refresh_token':stored['refresh_token']})
            stored = _credentials(current.provider, result, stored)
            current.oauth_credentials = _pack(stored)
            db.commit()
        set_committed_value(account, 'oauth_credentials', current.oauth_credentials)
        return stored['access_token']


def _connect_account(attempt, result):
    identity = _verify_identity(attempt['provider'], attempt['config'], result.get('id_token',''), attempt['nonce'])
    with _refresh_lock, Session() as db:
        provider = attempt['provider']
        account = db.scalar(select(Account).where(Account.provider == provider, Account.oauth_subject == identity['subject']))
        if attempt['account_id']:
            target = db.get(Account, attempt['account_id'])
            if not target or target.provider != provider or target.oauth_subject != identity['subject']:
                raise OAuthError('Choose the same email account when reconnecting. To connect another account, use the add-account button.')
            account = target
        if not account:
            # Upgrade an existing password-based entry only for this provider's exact mailbox host.
            account = db.scalar(select(Account).where(Account.username.ilike(identity['email']), Account.imap_host == PROVIDERS[provider]['imap'], Account.auth_type == 'password'))
        previous = _unpack(account) if account and account.auth_type == 'oauth2' and account.oauth_client_id == attempt['config']['client_id'] else None
        credentials = _credentials(provider, result, previous)
        if not account:
            account = Account(name=identity['name'], username=identity['email'], password=fernet.encrypt(b'').decode())
            db.add(account)
        account.auth_type = 'oauth2'; account.provider = provider
        account.oauth_subject = identity['subject']; account.oauth_client_id = attempt['config']['client_id']
        account.oauth_credentials = _pack(credentials)
        account.username = identity['email']; account.imap_host = PROVIDERS[provider]['imap']; account.imap_port = 993
        # Personal Microsoft mail uses the documented Outlook.com SMTP endpoint.
        account.smtp_host = 'smtp-mail.outlook.com' if provider == 'microsoft' and identity.get('consumer',False) else PROVIDERS[provider]['smtp']
        account.smtp_port = 587; account.sync_error = ''
        account.password = fernet.encrypt(b'').decode()
        db.commit()
        return account.id


def _page(handler, status, title, text):
    # All text is app-authored: never include codes, tokens, account data or provider-supplied HTML.
    from html import escape
    html = f'<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>FocusMail sign-in</title><style>body{{font:16px system-ui;background:#f7f8f5;color:#416c54;max-width:540px;margin:18vh auto;padding:30px;line-height:1.8}}h1{{font-size:28px}}p{{color:#68775d}}</style><h1>{escape(title)}</h1><p>{escape(text)}</p></html>'.encode()
    handler.send_response(status)
    for name,value in {'Content-Type':'text/html; charset=utf-8','Content-Length':str(len(html)), 'Cache-Control':'no-store','Referrer-Policy':'no-referrer','Content-Security-Policy':"default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'",'X-Content-Type-Options':'nosniff'}.items():
        handler.send_header(name,value)
    handler.end_headers(); handler.wfile.write(html)


def _stop(attempt):
    attempt['timer'].cancel()
    threading.Thread(target=attempt['server'].shutdown,daemon=True).start()


def _callback_handler(attempt):
    class Callback(BaseHTTPRequestHandler):
        def setup(self):
            super().setup(); self.connection.settimeout(10)
        def log_message(self, *_): pass
        def do_GET(self):
            parsed = urlsplit(self.path)
            try: query = parse_qs(parsed.query, max_num_fields=20)
            except ValueError:
                _page(self,400,'Invalid callback','Start sign-in again from FocusMail.'); return
            if parsed.path != '/oauth/callback' or len(self.path)>16000:
                _page(self,404,'Not found','Return to FocusMail to start sign-in.'); return
            supplied = query.get('state',[''])[0]
            if not hmac.compare_digest(supplied.encode(), attempt['state'].encode()):
                _page(self,400,'Sign-in could not be verified','This callback did not match your sign-in request. Return to the original sign-in page.'); return
            with _attempt_lock:
                if attempt['status'] != 'pending' or attempt['expires_at'] <= time.time():
                    _page(self,410,'Sign-in has ended','Start sign-in again from FocusMail.'); return
                attempt['status'] = 'processing'
            try:
                if 'error' in query: raise OAuthError('Sign-in was cancelled or denied by the provider. You can try again from FocusMail.')
                code = query.get('code',[''])[0]
                if not code: raise OAuthError('The provider did not return a sign-in code. Try again from FocusMail.')
                result = _token_request(attempt['provider'], attempt['config'], {'grant_type':'authorization_code', 'code':code, 'redirect_uri':attempt['redirect_uri'], 'code_verifier':attempt['verifier']})
                account_id = _connect_account(attempt,result)
                with _attempt_lock:
                    attempt.update(status='success', account_id=account_id, message='Account connected. Use Sync inbox when you’re ready.')
                _page(self,200,'You’re connected.','Return to FocusMail. Your mail will wait until you choose Sync inbox. You can close this tab.')
            except OAuthError as error:
                with _attempt_lock: attempt.update(status='error',message=str(error))
                _page(self,400,'Sign-in wasn’t completed',str(error))
            except Exception:
                log.warning('OAuth callback failed: provider=%s',attempt['provider'])
                with _attempt_lock: attempt.update(status='error',message='Sign-in could not be completed. Please try again.')
                _page(self,400,'Sign-in wasn’t completed','Return to FocusMail and try again.')
            finally:
                # Discard verifier/config secrets as soon as the attempt finishes.
                attempt['verifier']=''; attempt['config']={}; _stop(attempt)
    return Callback


def start(provider, owner, account_id=None):
    with Session() as db:
        config = configuration(db, provider)
        if not config['client_id']: raise OAuthError(f'Set up the {PROVIDERS[provider]["name"]} OAuth application first.')
        if account_id:
            account = db.get(Account,account_id)
            if not account or account.auth_type != 'oauth2' or account.provider != provider:
                raise OAuthError('This account cannot be reconnected with that provider.')
    with _attempt_lock:
        now = time.time()
        for key,item in list(_attempts.items()):
            if item['expires_at']+60 < now: _attempts.pop(key,None)
        if sum(a['status'] in ('pending','processing') for a in _attempts.values()) >= 4:
            raise OAuthError('A sign-in is already in progress. Complete or cancel it before starting another.')
        attempt = {'id':secrets.token_urlsafe(32), 'owner':hashlib.sha256(owner.encode()).hexdigest(),
                   'provider':provider,'config':config,'account_id':account_id, 'state':secrets.token_urlsafe(32),
                   'verifier':secrets.token_urlsafe(64),'nonce':secrets.token_urlsafe(32),
                   'status':'pending','expires_at':now+ATTEMPT_TTL,'message':'Waiting for sign-in in your browser.'}
        server = HTTPServer(('127.0.0.1',0),_callback_handler(attempt))
        attempt['server']=server
        host = '127.0.0.1' if provider == 'google' else 'localhost'
        attempt['redirect_uri']=f'http://{host}:{server.server_port}/oauth/callback'
        def expire():
            with _attempt_lock:
                if attempt['status'] in ('pending','processing'):
                    attempt.update(status='error',message='Sign-in timed out. Start again from FocusMail.')
                    attempt['verifier']=''; attempt['config']={}
            server.shutdown()
        timer = threading.Timer(ATTEMPT_TTL,expire); timer.daemon=True; attempt['timer']=timer
        _attempts[attempt['id']]=attempt
        def serve():
            try: server.serve_forever(poll_interval=0.2)
            finally: server.server_close()
        threading.Thread(target=serve,daemon=True).start(); timer.start()
        params = {'client_id':config['client_id'], 'redirect_uri':attempt['redirect_uri'], 'response_type':'code',
                  'scope':PROVIDERS[provider]['scope'], 'state':attempt['state'], 'nonce':attempt['nonce'],
                  'code_challenge':pkce_challenge(attempt['verifier']), 'code_challenge_method':'S256'}
        if provider == 'google': params.update(access_type='offline',prompt='consent select_account')
        else: params.update(prompt='select_account',response_mode='query')
        return {'attempt_id':attempt['id'],'authorization_url':PROVIDERS[provider]['authorize'].format(tenant=config['tenant'])+'?'+urlencode(params)}


def status(attempt_id, owner):
    with _attempt_lock:
        attempt = _attempts.get(attempt_id)
        if not attempt or not hmac.compare_digest(attempt['owner'],hashlib.sha256(owner.encode()).hexdigest()):
            raise OAuthError('Sign-in session not found. Start again from FocusMail.')
        return {key:attempt.get(key) for key in ('id','provider','status','message','account_id')}


def cancel(attempt_id,owner):
    status(attempt_id,owner)
    with _attempt_lock:
        attempt = _attempts[attempt_id]
        if attempt['status'] == 'processing': raise OAuthError('The provider response is being checked. Please wait a moment.')
        if attempt['status'] == 'pending':
            attempt.update(status='cancelled',message='Sign-in cancelled.'); attempt['verifier']=''; attempt['config']={}; _stop(attempt)


def close_all():
    with _attempt_lock:
        for attempt in _attempts.values():
            if attempt['status'] in ('pending','processing'):
                attempt.update(status='cancelled'); _stop(attempt)
        _attempts.clear()
