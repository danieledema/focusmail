import base64
import hashlib
import hmac
import os
import time
from cryptography.fernet import Fernet
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, RedirectResponse
from urllib.parse import urlparse
from .database import DATA_DIR

DEMO = os.getenv('DEMO_MODE', 'false').lower() == 'true'
PASSWORD = os.getenv('APP_PASSWORD', '')
DESKTOP = os.getenv('FOCUSMAIL_DESKTOP', 'false').lower() == 'true'
DESKTOP_TOKEN = os.getenv('FOCUSMAIL_DESKTOP_TOKEN', '')
key_file = DATA_DIR / 'credential.key'
if os.getenv('CREDENTIAL_KEY'):
    KEY = os.environ['CREDENTIAL_KEY'].encode()
elif key_file.exists():
    KEY = key_file.read_bytes()
else:
    KEY = Fernet.generate_key()
    fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as f:
        f.write(KEY)
fernet = Fernet(KEY)

def signature(value):
    return hmac.new(KEY + PASSWORD.encode(), value.encode(), hashlib.sha256).hexdigest()

def valid_cookie(cookie):
    try:
        stamp, sig = cookie.split('.')
        return 0 <= time.time() - int(stamp) < 86400 * 7 and hmac.compare_digest(sig, signature(stamp))
    except (ValueError, AttributeError):
        return False

class AuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app):
        super().__init__(app)
        self.desktop_bootstrapped = False

    async def dispatch(self, request, call_next):
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            origin = request.headers.get('origin')
            if origin and urlparse(origin).netloc != request.headers.get('host'):
                return JSONResponse({'detail': 'Cross-origin requests are not allowed'}, 403)
        if DESKTOP and request.url.path.startswith('/desktop/start/'):
            token = request.url.path.removeprefix('/desktop/start/')
            if self.desktop_bootstrapped or not DESKTOP_TOKEN or not hmac.compare_digest(token.encode(), DESKTOP_TOKEN.encode()):
                return JSONResponse({'detail':'Invalid desktop launch session'}, 401)
            self.desktop_bootstrapped = True
            response = RedirectResponse('/', status_code=303)
            stamp = str(int(time.time()))
            response.set_cookie('focusmail', stamp + '.' + signature(stamp), httponly=True, samesite='strict')
            response.headers['Cache-Control'] = 'no-store'
            response.headers['Referrer-Policy'] = 'no-referrer'
            return response
        if DEMO and not DESKTOP:
            return await call_next(request)
        authenticated = valid_cookie(request.cookies.get('focusmail', ''))
        if not authenticated and not DESKTOP:
            try:
                scheme, encoded = request.headers.get('authorization', '').split(' ', 1)
                _, password = base64.b64decode(encoded, validate=True).decode().split(':', 1)
                authenticated = scheme.lower() == 'basic' and hmac.compare_digest(password.encode(), PASSWORD.encode())
            except (ValueError, UnicodeError):
                pass
        if not authenticated and DESKTOP:
            return JSONResponse({'detail':'Open FocusMail from its desktop application.'}, 401)
        if not authenticated:
            return JSONResponse({'detail': 'Enter your FocusMail app password. Any username is accepted.'}, 401,
                                headers={'WWW-Authenticate': 'Basic realm="FocusMail", charset="UTF-8"'})
        response = await call_next(request)
        stamp = str(int(time.time()))
        response.set_cookie('focusmail', stamp + '.' + signature(stamp), httponly=True,
                            secure=request.url.scheme == 'https' or os.getenv('COOKIE_SECURE', 'false').lower() == 'true', samesite='strict', max_age=604800)
        if DESKTOP:
            response.headers['Cache-Control'] = 'no-store'
            response.headers['X-Content-Type-Options'] = 'nosniff'
            response.headers['Referrer-Policy'] = 'no-referrer'
            response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'"
        return response
