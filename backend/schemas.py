from typing import Literal
from pydantic import BaseModel, Field, field_validator

class AccountInput(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    username: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=1000)
    imap_host: str = Field(min_length=1, max_length=253, pattern=r'^[a-zA-Z0-9.\-]+$')
    imap_port: int = Field(default=993, ge=1, le=65535)
    smtp_host: str = Field(min_length=1, max_length=253, pattern=r'^[a-zA-Z0-9.\-]+$')
    smtp_port: int = Field(default=587, ge=1, le=65535)

class ActionInput(BaseModel):
    action: Literal['reply', 'delegate', 'snooze', 'archive', 'delete']
    session_id: str = Field(min_length=1, max_length=100)
    body: str = Field(default='', max_length=100000)
    archive_after_reply: bool = True
    archive_after_delegate: bool = True
    delegate_id: str = Field(default='', max_length=36)
    preset: Literal['hour', 'today', 'tomorrow', 'weekend', 'week', 'someday'] = 'tomorrow'
    timezone: str = 'Europe/London'

class Preferences(BaseModel):
    ai_timeout_seconds: int = Field(default=300, ge=30, le=1800)
    ai_base_url: str = Field(default='https://api.openai.com/v1', min_length=1, max_length=2000)
    ai_api_key: str | None = Field(default=None,max_length=1000)
    @field_validator('ai_base_url')
    @classmethod
    def valid_base_url(cls,value):
        from .services.ai_config import normalize_base_url
        return normalize_base_url(value)

    daily_goal: int = Field(default=10, ge=1, le=500)
    ai_model: str = Field(default='gpt-4o', min_length=1, max_length=100, pattern=r'^[a-zA-Z0-9._:/\-]+$')

class ChatInput(BaseModel):
    thread_id: int
    message: str = Field(min_length=1, max_length=8000)
    history: list[dict] = Field(default_factory=list, max_length=20)

class APIKeyInput(BaseModel):
    key: str = Field(default='', max_length=1000)

class AccountPasswordInput(BaseModel):
    password: str = Field(min_length=1, max_length=1000)

class OAuthConfiguration(BaseModel):
    client_id: str = Field(default='', max_length=300, pattern=r'^[a-zA-Z0-9._-]*$')
    client_secret: str | None = Field(default=None, max_length=1000)
    tenant: str = Field(default='common', max_length=253, pattern=r'^[a-zA-Z0-9.-]+$')

class OAuthStart(BaseModel):
    account_id: int | None = None

class DelegatePerson(BaseModel):
    id: str = Field(default_factory=lambda: __import__('uuid').uuid4().__str__(), min_length=1,max_length=36)
    name: str = Field(min_length=1,max_length=100)
    email: str = Field(min_length=3,max_length=254)

    @field_validator('name')
    @classmethod
    def valid_name(cls,value):
        value=value.strip()
        if not value or any(ord(c)<32 for c in value): raise ValueError('Enter a name without control characters.')
        return value

    @field_validator('email')
    @classmethod
    def valid_email(cls,value):
        import re
        value=value.strip()
        if not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?",value):
            raise ValueError('Enter one email address, without a display name.')
        local,domain=value.rsplit('@',1)
        if len(local)>64 or local.startswith('.') or local.endswith('.') or '..' in local or any(not label or label.startswith('-') or label.endswith('-') or len(label)>63 for label in domain.split('.')):
            raise ValueError('Enter a valid email address.')
        return local+'@'+domain.lower()

class DelegatesInput(BaseModel):
    people: list[DelegatePerson] = Field(default_factory=list,max_length=100)
