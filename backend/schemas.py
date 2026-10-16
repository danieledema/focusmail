from typing import Literal
from pydantic import BaseModel, Field

class AccountInput(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    username: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=1000)
    imap_host: str = Field(min_length=1, max_length=253, pattern=r'^[a-zA-Z0-9.\-]+$')
    imap_port: int = Field(default=993, ge=1, le=65535)
    smtp_host: str = Field(min_length=1, max_length=253, pattern=r'^[a-zA-Z0-9.\-]+$')
    smtp_port: int = Field(default=587, ge=1, le=65535)

class ActionInput(BaseModel):
    action: Literal['reply', 'snooze', 'archive', 'delete']
    session_id: str = Field(min_length=1, max_length=100)
    body: str = Field(default='', max_length=100000)
    preset: Literal['hour', 'today', 'tomorrow', 'weekend', 'week', 'someday'] = 'tomorrow'
    timezone: str = 'Europe/London'

class Preferences(BaseModel):
    daily_goal: int = Field(default=10, ge=1, le=500)
    poll_interval: int = Field(default=5, ge=1, le=1440)
    ai_model: str = Field(default='gpt-4o', min_length=1, max_length=100, pattern=r'^[a-zA-Z0-9._:/\-]+$')

class ChatInput(BaseModel):
    thread_id: int
    message: str = Field(min_length=1, max_length=8000)
    history: list[dict] = Field(default_factory=list, max_length=20)
