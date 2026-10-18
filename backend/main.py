import asyncio
import json
import logging
import os
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from zoneinfo import ZoneInfoNotFoundError
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, delete
from .database import Base, engine, Session, initialize_database
from .models import Account, Thread, Message, Setting, ActionEvent, PendingDelete, Badge
from .auth import AuthMiddleware, DEMO, PASSWORD, DESKTOP, fernet
from .schemas import AccountInput, ActionInput, Preferences, ChatInput, APIKeyInput, AccountPasswordInput, DelegatesInput
from .services import imap_service, smtp_service, ai_service, oauth_service
from .services.game_service import game_state, get_setting, today
from .services.snooze_scheduler import snooze_time
from .demo import seed
from .services.connection_diagnostics import connection_error
from .secrets import ai_api_key, saved_api_key, set_api_key
from .routers.oauth import router as oauth_router
from .services.ai_config import configured_base_url, uses_openai

logger = logging.getLogger('focusmail')
mutation_lock = threading.RLock()
sync_status_lock = threading.RLock()
sync_status = {'running': False, 'folder': '', 'completed': 0, 'total': 0, 'error': False}

def sync_snapshot():
    with sync_status_lock:
        return dict(sync_status)

def sync_progress(**values):
    with sync_status_lock:
        sync_status.update(values)


def prefs(db):
    return {**{key: get_setting(db, key, default) for key, default in [('ai_timeout_seconds',300), ('daily_goal',10), ('ai_model',os.getenv('AI_MODEL','gpt-4o'))]}, 'ai_base_url':configured_base_url(db)}

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

def run_maintenance():
    """Advance local snoozes and finish user-requested deletes; never fetch mail."""
    with mutation_lock, Session() as db:
        awaken(db); db.commit(); finalize_deletes(db)

def synchronize():
    """Run only after an explicit Sync inbox request; never on a timer."""
    try:
        if DEMO: return
        with Session() as db:
            with mutation_lock:
                account_ids = list(db.scalars(select(Account.id)))
                db.commit()
            for account_id in account_ids:
                with mutation_lock:
                    account = db.get(Account, account_id)
                    if account is None: continue
                    account.sync_error = ''
                    db.commit()
                try:
                    imap_service.sync_account(db, account, mutation_lock, sync_progress)
                except Exception as error:
                    problem = connection_error(error)
                    logger.warning('Sync failed: account=%s code=%s', account_id, problem['code'])
                    with mutation_lock:
                        db.rollback(); account = db.get(Account, account_id)
                        if account:
                            account.sync_error = problem['message']
                            db.commit()
                    sync_progress(error=True)
    except Exception:
        logger.warning('Sync could not finish')
        sync_progress(error=True)
    finally:
        sync_progress(running=False)

async def scheduler():
    while True:
        try: await asyncio.to_thread(run_maintenance)
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
    oauth_service.close_all()
    task.cancel()
    try: await task
    except asyncio.CancelledError: pass

app = FastAPI(title='FocusMail', lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(AuthMiddleware)
app.include_router(oauth_router)

def account_json(a):
    return {key:getattr(a,key) for key in ['id','name','username','imap_host','imap_port','smtp_host','smtp_port','last_sync','sync_error','auth_type','provider']}

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
        counts = {status:len(list(db.scalars(select(Thread.id).where(Thread.status == status)))) for status in ['inbox','snoozed','archived','replied','delegated','deleted']}
        errors = [p.error for p in db.scalars(select(PendingDelete).where(PendingDelete.error != ''))]
        return {'demo':DEMO, 'desktop':DESKTOP, 'ai_ready':DEMO or not uses_openai(db) or bool(ai_api_key(db)), 'ai_key_saved':bool(saved_api_key(db)), 'accounts':[account_json(a) for a in db.scalars(select(Account))],
                'delegates':get_setting(db,'delegates',[]), 'sync':sync_snapshot(), 'preferences':prefs(db), 'counts':counts, 'thread':thread_json(db, queue[0]) if queue else None,
                'game':game_state(db, session_id), 'errors':errors}

@app.get('/api/threads')
def thread_list(status: str = 'snoozed'):
    if status not in ('snoozed', 'archived', 'replied', 'delegated', 'deleted'): raise HTTPException(400, 'Invalid collection')
    with Session() as db:
        return [thread_json(db,t) for t in db.scalars(select(Thread).where(Thread.status == status).order_by(Thread.oldest))]

@app.post('/api/threads/{thread_id}/wake')
def wake(thread_id: int):
    with mutation_lock, Session() as db:
        thread = db.get(Thread, thread_id)
        if not thread or thread.status != 'snoozed': raise HTTPException(409, 'This thread is no longer snoozed')
        thread.status = 'inbox'; thread.resurfaced_at = time.time(); thread.snoozed_until = None; db.commit()
        return {'ok':True}

@app.put('/api/delegates')
def save_delegates(data: DelegatesInput):
    people=[person.model_dump() for person in data.people]
    if len({p['id'] for p in people})!=len(people) or len({p['email'].lower() for p in people})!=len(people):
        raise HTTPException(400,'Each person needs a unique email address.')
    with mutation_lock, Session() as db:
        db.merge(Setting(key='delegates',value=people)); db.commit()
    return {'people':people}

@app.post('/api/threads/{thread_id}/action')
def take_action(thread_id: int, data: ActionInput):
    with mutation_lock, Session() as db:
        thread = db.get(Thread, thread_id)
        if not thread or thread.status != 'inbox': raise HTTPException(409, 'This thread has already been processed. Refresh your queue.')
        warning = None
        reply_archived = False
        if data.action == 'reply':
            if not data.body.strip(): raise HTTPException(400, 'Write a reply before sending')
            account = db.get(Account, thread.account_id)
            latest = db.scalar(select(Message).where(Message.thread_id == thread.id, Message.sender != account.username).order_by(Message.date.desc()))
            if not latest: raise HTTPException(400, 'No recipient found for this thread')
            try: message_id = '<demo-reply>' if DEMO else smtp_service.send_reply(account, latest, thread.subject, data.body)
            except Exception:
                logger.exception('SMTP failed'); raise HTTPException(502, 'Reply was not sent. Check your SMTP settings and try again.')
            db.add(Message(thread_id=thread.id, account_id=account.id, uid='sent-'+str(uuid.uuid4()), uidvalidity='local', folder='Sent',
                           message_id=message_id, sender=account.username, sender_name='You', recipient=latest.sender, body=data.body, date=time.time()))
            thread.status = 'replied' if data.archive_after_reply else 'inbox'
        elif data.action == 'delegate':
            if not data.body.strip(): raise HTTPException(400,'Write a message for the person you are delegating to.')
            person=next((p for p in get_setting(db,'delegates',[]) if p['id']==data.delegate_id),None)
            if not person: raise HTTPException(400,'Choose a saved person before delegating.')
            account=db.get(Account,thread.account_id)
            latest=db.scalar(select(Message).where(Message.thread_id==thread.id,Message.sender!=account.username).order_by(Message.date.desc(),Message.id.desc()))
            if not latest: raise HTTPException(400,'No received message found to forward.')
            body=smtp_service.forward_body(latest,thread.subject,data.body)
            try:
                message_id='<demo-forward-'+str(uuid.uuid4())+'>' if DEMO else smtp_service.send_forward(account,latest,thread.subject,person['email'],data.body)
            except Exception:
                logger.exception('Delegation SMTP failed'); raise HTTPException(502,'Forward was not sent. Check your SMTP settings before trying again.')
            db.add(Message(thread_id=thread.id,account_id=account.id,uid='sent-'+str(uuid.uuid4()),uidvalidity='local',folder='Sent',
                           message_id=message_id,sender=account.username,sender_name='You',recipient=person['email'],body=body,date=time.time()))
            thread.status='delegated' if data.archive_after_delegate else 'inbox'
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
        for stale in db.scalars(select(PendingDelete).where(PendingDelete.thread_id == thread_id, PendingDelete.error != '')):
            db.delete(stale)
        points = {'reply':3, 'delegate':3, 'archive':2, 'delete':1, 'snooze':-1}[data.action]
        event = ActionEvent(thread_id=thread.id, session_id=data.session_id, action=data.action, at=time.time(), day=today().isoformat(), points=points)
        db.add(event); db.flush()
        undo = None
        if data.action == 'delete':
            undo = str(uuid.uuid4()); db.add(PendingDelete(id=undo, thread_id=thread.id, event_id=event.id, due=time.time()+15))
        game = game_state(db, data.session_id, unlock=True); db.commit()
        send_archived=False
        if data.action in ('reply','delegate') and (data.archive_after_reply if data.action=='reply' else data.archive_after_delegate):
            try:
                if not DEMO: imap_service.move_thread(db, thread, 'archive')
                reply_archived = data.action=='reply'
                send_archived = True
            except Exception:
                logger.exception('Message sent, archive failed'); warning = ('Reply' if data.action=='reply' else 'Forward')+' sent successfully. The original thread could not be archived on your mail server. Do not send it again.'
        return {'ok':True, 'undo_token':undo, 'undo_seconds':15, 'game':game, 'warning':warning, 'reply_archived':reply_archived, 'delegate_archived':data.action=='delegate' and send_archived}

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
    with sync_status_lock:
        if not sync_status['running']:
            sync_status.update(running=True, folder='', completed=0, total=0, error=False)
            threading.Thread(target=synchronize, daemon=True, name='focusmail-manual-sync').start()
    return {'ok':True, 'sync':sync_snapshot()}

@app.get('/api/sync')
def sync_state():
    return sync_snapshot()

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

@app.put('/api/accounts/{account_id}/password')
def update_account_password(account_id: int, data: AccountPasswordInput):
    if DEMO: raise HTTPException(400, 'Sample account passwords cannot be changed')
    with mutation_lock, Session() as db:
        account = db.get(Account, account_id)
        if not account: raise HTTPException(404, 'Account not found')
        if account.auth_type == 'oauth2': raise HTTPException(409, 'Reconnect this account with its provider instead of changing a password.')
        account.password = fernet.encrypt(data.password.encode()).decode()
        account.sync_error = ''
        db.commit()
        return account_json(account)

@app.post('/api/accounts/{account_id}/test')
def test_account(account_id: int):
    if DEMO: return {'ok':True, 'message':'Sample account is ready. No mail server was contacted.', 'checks':[]}
    with mutation_lock, Session() as db:
        account = db.get(Account, account_id)
        if not account: raise HTTPException(404, 'Account not found')
        checks = []
        for service in ('IMAP', 'SMTP'):
            try:
                if service == 'IMAP':
                    client = imap_service.connect(account)
                    try: client.noop()
                    finally: client.logout()
                else:
                    smtp_service.test_smtp(account)
                checks.append({'service':service, 'status':'ok', 'code':'connected', 'message':f'{service}: secure connection and login succeeded.'})
            except Exception as error:
                problem = connection_error(error, service)
                checks.append(problem)
                if service == 'IMAP':
                    checks.append({'service':'SMTP', 'status':'skipped', 'code':'not_tested', 'message':'SMTP: not tested yet. Resolve the IMAP connection first.'})
                account.sync_error = problem['message']
                db.commit()
                logger.warning('Connection test failed: account=%s service=%s code=%s', account.id, service, problem['code'])
                raise HTTPException(502, {'message':problem['message'], 'checks':checks})
        account.sync_error = ''
        db.commit()
        return {'ok':True, 'message':'IMAP and SMTP connections are working. No email was sent or fetched.', 'checks':checks}

@app.put('/api/preferences')
def update_preferences(data: Preferences):
    with mutation_lock, Session() as db:
        if DEMO and data.ai_api_key is not None: raise HTTPException(400,'API keys are disabled in demo mode')
        for key, value in data.model_dump(exclude={'ai_api_key'}).items():
            db.merge(Setting(key=key, value=value))
        if data.ai_api_key is not None: set_api_key(db,data.ai_api_key.strip())
        else: db.commit()
        return prefs(db)

@app.put('/api/ai/key')
def save_ai_key(data: APIKeyInput):
    if DEMO: raise HTTPException(400, 'API keys are disabled in demo mode')
    with mutation_lock, Session() as db:
        set_api_key(db, data.key.strip())
        return {'configured':bool(ai_api_key(db)), 'saved':bool(saved_api_key(db))}

@app.post('/api/ai/chat/stream')
async def ai_chat_stream(data: ChatInput):
    # Validate before opening the response; own the session for the stream's lifetime.
    with Session() as db:
        if not db.get(Thread,data.thread_id): raise HTTPException(404,'Thread not found')
    async def events():
        with Session() as db:
            thread=db.get(Thread,data.thread_id)
            messages=list(db.scalars(select(Message).where(Message.thread_id==data.thread_id).order_by(Message.date)))
            if DEMO:
                yield json.dumps({'type':'status','stage':'waiting'})+'\n'
                await asyncio.sleep(.6)
                yield json.dumps({'type':'result',**ai_service.demo_chat(messages,data.message)})+'\n'
            else:
                stream=ai_service.stream_chat(db,messages,data.message,data.history,thread.subject)
                try:
                    async for event in stream:
                        yield json.dumps(event)+'\n'
                finally: await stream.aclose()
    return StreamingResponse(events(),media_type='application/x-ndjson',headers={'Cache-Control':'no-store','X-Accel-Buffering':'no'})

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
