from sqlalchemy import Column, Integer, String, Text, Float, ForeignKey, JSON, UniqueConstraint
from .database import Base

class Account(Base):
    __tablename__ = 'accounts'
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    username = Column(String, nullable=False)
    password = Column(Text, nullable=False)
    imap_host = Column(String, nullable=False)
    imap_port = Column(Integer, default=993)
    smtp_host = Column(String, nullable=False)
    smtp_port = Column(Integer, default=587)
    last_sync = Column(Float, default=0)
    sync_error = Column(String, default='')
    auth_type = Column(String, default='password', server_default='password', nullable=False)
    provider = Column(String, default='manual', server_default='manual', nullable=False)
    oauth_subject = Column(String, nullable=True)
    oauth_client_id = Column(String, nullable=True)
    oauth_credentials = Column(Text, nullable=True)

class Thread(Base):
    __tablename__ = 'threads'
    id = Column(Integer, primary_key=True)
    account_id = Column(Integer, ForeignKey('accounts.id', ondelete='CASCADE'), nullable=False)
    subject = Column(String, nullable=False)
    status = Column(String, default='inbox')
    oldest = Column(Float, nullable=False)
    snoozed_until = Column(Float, nullable=True)
    resurfaced_at = Column(Float, nullable=True)

class Message(Base):
    __tablename__ = 'messages'
    __table_args__ = (UniqueConstraint('account_id', 'uid', 'uidvalidity'),)
    id = Column(Integer, primary_key=True)
    thread_id = Column(Integer, ForeignKey('threads.id', ondelete='CASCADE'))
    account_id = Column(Integer, ForeignKey('accounts.id', ondelete='CASCADE'))
    uid = Column(String)
    uidvalidity = Column(String, default='')
    folder = Column(String, default='INBOX', server_default='INBOX')
    message_id = Column(String)
    references = Column(Text, default='')
    sender = Column(String)
    sender_name = Column(String)
    recipient = Column(String)
    date = Column(Float)
    body = Column(Text)
    body_version = Column(Integer, default=1, server_default='1', nullable=False)

class Setting(Base):
    __tablename__ = 'settings'
    key = Column(String, primary_key=True)
    value = Column(JSON)

class ActionEvent(Base):
    __tablename__ = 'events'
    id = Column(Integer, primary_key=True)
    thread_id = Column(Integer)
    session_id = Column(String)
    action = Column(String)
    at = Column(Float)
    day = Column(String)
    points = Column(Integer)
    undone = Column(Integer, default=0)

class PendingDelete(Base):
    __tablename__ = 'pending_deletes'
    id = Column(String, primary_key=True)
    thread_id = Column(Integer, ForeignKey('threads.id', ondelete='CASCADE'))
    event_id = Column(Integer, ForeignKey('events.id'))
    due = Column(Float)
    error = Column(String, default='')

class Badge(Base):
    __tablename__ = 'badges'
    key = Column(String, primary_key=True)
    unlocked_at = Column(Float)
