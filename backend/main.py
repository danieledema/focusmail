import asyncio
import logging
import os
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from zoneinfo import ZoneInfoNotFoundError
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, delete
from .database import Base, engine, Session, initialize_database
from .models import Account, Thread, Message, Setting, ActionEvent, PendingDelete, Badge
from .auth import AuthMiddleware, DEMO, PASSWORD, fernet
from .schemas import AccountInput, ActionInput, Preferences, ChatInput
from .services import imap_service, smtp_service, ai_service
from .services.game_service import game_state, get_setting, today
from .services.snooze_scheduler import snooze_time
from .demo import seed

logger = logging.getLogger('focusmail')
mutation_lock = threading.RLock()

def prefs(db):
    return {key: get_setting(db, key, default) for key, default in [('daily_goal',10), ('poll_interval',5), ('ai_model',os.getenv('AI_MODEL','gpt-4o'))]}

def awaken(db):
    for thread in db.scalars(select(Thread).where(Thread.status == 'snoozed', Thread.snoozed_until <= time.time())):
        thread.status = 'inbox'; thread.resurfaced_at = time.time(); thread.snoozed_until = None

def finalize_deletes(db):
    for pending in list(db.scalars(select(PendingDelete).where(PendingDelete.due <= time.time()))):
        thread = db.get(Thread, pending.thread_id)
        try:
            if not DEMO: imap_service.move_thread(db, thread, 'delete')
            thread.status = 'deleted'; db.delete(pending); db.commit()
        except Exception:
            logger.exception('Deferred delete failed for thread %s', thread.id)
            pending.error = 'Could not move to Trash. Restored to your queue; check the account connection.'
            thread.status = 'inbox'
            action_event = db.get(ActionEvent, pending.event_id)
            if action_event: action_event.undone = 1
            pending.due = time.time() + 86400 * 365
            db.commit()

def synchronize(force=False):
    with mutation_lock, Session() as db:
        awaken(db); db.commit(); finalize_deletes(db)
        if DEMO: return
        for account in list(db.scalars(select(Account))):
            if force or time.time() - account.last_sync >= prefs(db)['poll_interval'] * 60:
                try:
                    imap_service.sync_account(db, account); db.commit()
                except Exception:
                    logger.exception('Sync failed for account %s', account.id)
                    db.rollback(); account = db.get(Account, account.id)
                    account.sync_error = 'Sync failed. Check your server, app password, and connection.'
                    account.last_sync = time.time(); db.commit()

async def scheduler():
    while True:
        try: await asyncio.to_thread(synchronize)
        except Exception: logger.exception('Scheduler error')
        await asyncio.sleep(10)

@asynccontextmanager
async def lifespan(app):
    if not DEMO and not PASSWORD:
        raise RuntimeError('Set APP_PASSWORD before starting FocusMail, or explicitly set DEMO_MODE=true for a sample inbox.')
    initialize_database()
    if DEMO:
        with Session() as db: seed(db)
    task = asyncio.create_task(scheduler())
    yield
    task.cancel()
    try: await task
    except asyncio.CancelledError: pass

app = FastAPI(title='FocusMail', lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(AuthMiddleware)

def account_json(a):
    return {key:getattr(a,key) for key in ['id','name','username','imap_host','imap_port','smtp_host','smtp_port','last_sync','sync_error']}

def thread_json(db, thread):
    account = db.get(Account, thread.account_id)
    messages = list(db.scalars(select(Message).where(Message.thread_id == thread.id).order_by(Message.date, Message.id)))
    return {'id':thread.id, 'subject':thread.subject, 'status':thread.status, 'snoozed_until':thread.snoozed_until,
            'account':account_json(account), 'messages':[{k:getattr(m,k) for k in ['id','sender','sender_name','recipient','date','body']} for m in messages]}

@app.get('/api/state')
def state(session_id: str = ''):
    with mutation_lock, Session() as db:
        awaken(db); db.commit()
        queue = list(db.scalars(select(Thread).where(Thread.status == 'inbox').order_by(Thread.resurfaced_at.desc(), Thread.oldest)))
        counts = {status:len(list(db.scalars(select(Thread.id).where(Thread.status == status)))) for status in ['inbox','snoozed','archived','replied','deleted']}
        errors = [p.error for p in db.scalars(select(PendingDelete).where(PendingDelete.error != ''))]
        return {'demo':DEMO, 'ai_ready':DEMO or bool(os.getenv('OPENAI_API_KEY')), 'accounts':[account_json(a) for a in db.scalars(select(Account))],
                'preferences':prefs(db), 'counts':counts, 'thread':thread_json(db, queue[0]) if queue else None,
                'game':game_state(db, session_id), 'errors':errors}

@app.get('/api/threads')
def thread_list(status: str = 'snoozed'):
    if status not in ('snoozed', 'archived', 'replied', 'deleted'): raise HTTPException(400, 'Invalid collection')
    with Session() as db:
        return [thread_json(db,t) for t in db.scalars(select(Thread).where(Thread.status == status).order_by(Thread.oldest))]

@app.post('/api/threads/{thread_id}/wake')
def wake(thread_id: int):
    with mutation_lock, Session() as db:
        thread = db.get(Thread, thread_id)
        if not thread or thread.status != 'snoozed': raise HTTPException(409, 'This thread is no longer snoozed')
        thread.status = 'inbox'; thread.resurfaced_at = time.time(); thread.snoozed_until = None; db.commit()
        return {'ok':True}

@app.post('/api/threads/{thread_id}/action')
def take_action(thread_id: int, data: ActionInput):
    with mutation_lock, Session() as db:
        thread = db.get(Thread, thread_id)
        if not thread or thread.status != 'inbox': raise HTTPException(409, 'This thread has already been processed. Refresh your queue.')
        warning = None
        for stale in db.scalars(select(PendingDelete).where(PendingDelete.thread_id == thread_id, PendingDelete.error != '')):
            db.delete(stale)
        if data.action == 'reply':
            if not data.body.strip(): raise HTTPException(400, 'Write a reply before sending')
            account = db.get(Account, thread.account_id)
            latest = db.scalar(select(Message).where(Message.thread_id == thread.id, Message.sender != account.username).order_by(Message.date.desc()))
            if not latest: raise HTTPException(400, 'No recipient found for this thread')
            try: message_id = '<demo-reply>' if DEMO else smtp_service.send_reply(account, latest, thread.subject, data.body)
            except Exception:
                logger.exception('SMTP failed'); raise HTTPException(502, 'Reply was not sent. Check your SMTP settings and try again.')
            db.add(Message(thread_id=thread.id, account_id=account.id, uid='sent-'+str(uuid.uuid4()), uidvalidity='local',
                           message_id=message_id, sender=account.username, sender_name='You', recipient=latest.sender, body=data.body, date=time.time()))
            thread.status = 'replied'
        elif data.action == 'archive':
            try:
                if not DEMO: imap_service.move_thread(db, thread, 'archive')
            except Exception:
                logger.exception('Archive failed'); raise HTTPException(502, 'Could not archive this thread. Check your account connection and Archive folder.')
            thread.status = 'archived'
        elif data.action == 'snooze':
            try: thread.snoozed_until = snooze_time(data.preset, data.timezone)
            except (ZoneInfoNotFoundError, ValueError): raise HTTPException(400, 'Unknown time zone')
            thread.status = 'snoozed'
        else: thread.status = 'pending_delete'
        points = {'reply':3, 'archive':2, 'delete':1, 'snooze':-1}[data.action]
        event = ActionEvent(thread_id=thread.id, session_id=data.session_id, action=data.action, at=time.time(), day=today().isoformat(), points=points)
        db.add(event); db.flush()
        undo = None
        if data.action == 'delete':
            undo = str(uuid.uuid4()); db.add(PendingDelete(id=undo, thread_id=thread.id, event_id=event.id, due=time.time()+15))
        game = game_state(db, data.session_id, unlock=True); db.commit()
        if data.action == 'reply' and not DEMO:
            try: imap_service.move_thread(db, thread, 'archive')
            except Exception:
                logger.exception('Reply sent, archive failed'); warning = 'Reply sent successfully. The original thread could not be archived on your mail server.'
        return {'ok':True, 'undo_token':undo, 'undo_seconds':15, 'game':game, 'warning':warning}

@app.post('/api/undo/{token}')
def undo_delete(token: str):
    with mutation_lock, Session() as db:
        pending = db.get(PendingDelete, token)
        if not pending or pending.due <= time.time() or pending.error: raise HTTPException(409, 'The undo window has closed')
        thread = db.get(Thread, pending.thread_id); thread.status = 'inbox'; thread.resurfaced_at = time.time()
        db.get(ActionEvent, pending.event_id).undone = 1
        db.delete(pending); db.commit(); return {'ok':True}

@app.post('/api/sync')
def sync():
    synchronize(force=True)
    return {'ok':True}

@app.post('/api/accounts')
def add_account(data: AccountInput):
    if DEMO: raise HTTPException(400, 'Account connections are disabled in demo mode. Start with DEMO_MODE=false to connect your email.')
    with mutation_lock, Session() as db:
        account = Account(**{**data.model_dump(), 'password':fernet.encrypt(data.password.encode()).decode()})
        db.add(account); db.commit(); return account_json(account)

@app.delete('/api/accounts/{account_id}')
def remove_account(account_id: int):
    if DEMO: raise HTTPException(400, 'The sample account stays connected in demo mode')
    with mutation_lock, Session() as db:
        account = db.get(Account, account_id)
        if not account: raise HTTPException(404, 'Account not found')
        db.delete(account); db.commit(); return {'ok':True}

@app.post('/api/accounts/{account_id}/test')
def test_account(account_id: int):
    if DEMO: return {'ok':True, 'message':'Sample account is ready. No mail server was contacted.'}
    with Session() as db:
        account = db.get(Account, account_id)
        if not account: raise HTTPException(404, 'Account not found')
        try:
            client = imap_service.connect(account); client.logout(); smtp_service.test_smtp(account)
        except Exception:
            logger.exception('Connection test failed'); raise HTTPException(502, 'Connection failed. Check IMAP/SMTP hosts, ports, and your app password.')
        return {'ok':True, 'message':'IMAP and SMTP connections are working.'}

@app.put('/api/preferences')
def update_preferences(data: Preferences):
    with mutation_lock, Session() as db:
        for key, value in data.model_dump().items():
            db.merge(Setting(key=key, value=value))
        db.commit(); return prefs(db)

@app.post('/api/ai/chat')
async def ai_chat(data: ChatInput):
    with Session() as db:
        thread = db.get(Thread, data.thread_id)
        if not thread: raise HTTPException(404, 'Thread not found')
        messages = list(db.scalars(select(Message).where(Message.thread_id == data.thread_id).order_by(Message.date)))
        if DEMO:
            await asyncio.sleep(0.6)
            return ai_service.demo_chat(messages, data.message)
        try: return await ai_service.chat(db, messages, data.message, data.history, thread.subject)
        except ValueError as error: raise HTTPException(503, str(error))
        except Exception:
            logger.exception('AI request failed'); raise HTTPException(502, 'The thinking partner is unavailable right now. Please try again.')

@app.post('/api/demo/reset')
def reset_demo():
    if not DEMO: raise HTTPException(404)
    with mutation_lock, Session() as db:
        for model in [PendingDelete, ActionEvent, Badge, Message, Thread, Account]: db.execute(delete(model))
        db.commit(); seed(db)
        return {'ok':True}

DIST = Path(__file__).resolve().parents[1] / 'frontend' / 'dist'
if DIST.exists():
    app.mount('/assets', StaticFiles(directory=DIST / 'assets'), name='assets')
    @app.get('/favicon.svg')
    def favicon(): return FileResponse(DIST / 'favicon.svg')
    @app.get('/{path:path}')
    def spa(path: str):
        if path.startswith('api/'): raise HTTPException(404)
        return FileResponse(DIST / 'index.html')
