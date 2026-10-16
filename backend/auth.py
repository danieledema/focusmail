import base64
import hashlib
import hmac
import os
import time
from cryptography.fernet import Fernet
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from urllib.parse import urlparse
from .database import DATA_DIR

DEMO = os.getenv('DEMO_MODE', 'false').lower() == 'true'
PASSWORD = os.getenv('APP_PASSWORD', '')
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
    async def dispatch(self, request, call_next):
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            origin = request.headers.get('origin')
            if origin and urlparse(origin).netloc != request.headers.get('host'):
                return JSONResponse({'detail': 'Cross-origin requests are not allowed'}, 403)
        if DEMO:
            return await call_next(request)
        authenticated = valid_cookie(request.cookies.get('focusmail', ''))
        if not authenticated:
            try:
                scheme, encoded = request.headers.get('authorization', '').split(' ', 1)
                _, password = base64.b64decode(encoded, validate=True).decode().split(':', 1)
                authenticated = scheme.lower() == 'basic' and hmac.compare_digest(password.encode(), PASSWORD.encode())
            except (ValueError, UnicodeError):
                pass
        if not authenticated:
            return JSONResponse({'detail': 'Enter your FocusMail app password. Any username is accepted.'}, 401,
                                headers={'WWW-Authenticate': 'Basic realm="FocusMail", charset="UTF-8"'})
        response = await call_next(request)
        stamp = str(int(time.time()))
        response.set_cookie('focusmail', stamp + '.' + signature(stamp), httponly=True,
                            secure=request.url.scheme == 'https' or os.getenv('COOKIE_SECURE', 'false').lower() == 'true', samesite='strict', max_age=604800)
        return response
