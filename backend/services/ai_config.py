import os
import re
from urllib.parse import urlsplit, urlunsplit

DEFAULT_AI_BASE_URL = 'https://api.openai.com/v1'

def normalize_base_url(value):
    value=value.strip().rstrip('/')
    if re.search(r'[\s\x00-\x1f\x7f]', value):
        raise ValueError('Use an HTTP or HTTPS API base URL without spaces.')
    try:
        url=urlsplit(value)
        if url.scheme not in ('http','https') or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError()
        port=url.port
        host=url.hostname.lower()
        if ':' in host: host='['+host+']'
        authority=host+((':'+str(port)) if port and not (url.scheme=='https' and port==443 or url.scheme=='http' and port==80) else '')
        path=url.path.rstrip('/')
        if path.endswith('/chat/completions'): path=path[:-len('/chat/completions')]
        return urlunsplit((url.scheme,authority,path,'',''))
    except ValueError:
        raise ValueError('Use an HTTP or HTTPS API base URL without credentials, query parameters, or a fragment.') from None

def configured_base_url(db):
    from .game_service import get_setting
    return normalize_base_url(get_setting(db,'ai_base_url',os.getenv('OPENAI_BASE_URL',DEFAULT_AI_BASE_URL)))

def uses_openai(db):
    return urlsplit(configured_base_url(db)).hostname=='api.openai.com'
