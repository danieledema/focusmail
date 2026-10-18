"""App-owned, platform-independent encrypted secrets; never returned by settings APIs."""
import os
import hashlib
from .services.ai_config import configured_base_url, normalize_base_url, DEFAULT_AI_BASE_URL
from .auth import fernet
from .models import Setting

API_KEY_SETTING = 'secret.openai_api_key'

def key_setting(db):
    base=configured_base_url(db)
    return API_KEY_SETTING if base==DEFAULT_AI_BASE_URL else 'secret.ai_api_key.'+hashlib.sha256(base.encode()).hexdigest()

def saved_api_key(db):
    row = db.get(Setting, key_setting(db))
    return fernet.decrypt(row.value.encode()).decode() if row else None

def ai_api_key(db):
    saved=saved_api_key(db)
    environment_base=normalize_base_url(os.getenv('OPENAI_BASE_URL',DEFAULT_AI_BASE_URL))
    return saved or (os.getenv('OPENAI_API_KEY') if configured_base_url(db)==environment_base else None)

def set_api_key(db, value):
    setting=key_setting(db)
    row = db.get(Setting, setting)
    if not value:
        if row: db.delete(row)
    else:
        db.merge(Setting(key=setting, value=fernet.encrypt(value.encode()).decode()))
    db.commit()
