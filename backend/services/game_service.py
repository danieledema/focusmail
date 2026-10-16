import os
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from sqlalchemy import select
from ..models import ActionEvent, Badge, Setting, Thread
BADGES = [
    ('first_reply', 'First Reply', 'Sent your first reply', 'mail'),
    ('decisive', 'Decisive', 'Processed 10 threads in a session', 'target'),
    ('streak_3', '3-Day Streak', 'Three days of showing up', 'flame'),
    ('streak_7', 'Week Warrior', 'A seven-day streak', 'calendar'),
    ('speed', 'Speed Round', 'Five threads in five minutes', 'zap'),
    ('zero', 'Inbox Zero', 'A fresh start for your inbox', 'trophy'),
    ('century', 'Century', 'Processed 100 threads', 'star')]

def get_setting(db, key, default):
    row = db.get(Setting, key)
    return row.value if row else default

def today():
    return datetime.now(ZoneInfo(os.getenv('TZ', 'Europe/London'))).date()

def game_state(db, session_id='', unlock=False):
    events = list(db.scalars(select(ActionEvent).where(ActionEvent.undone == 0).order_by(ActionEvent.at)))
    session = [e for e in events if e.session_id == session_id]
    daily = [e for e in events if e.day == today().isoformat()]
    days = {e.day for e in events}
    cursor = today() if today().isoformat() in days else today() - timedelta(days=1)
    streak = 0
    while cursor.isoformat() in days:
        streak += 1; cursor -= timedelta(days=1)
    queue = list(db.scalars(select(Thread).where(Thread.status == 'inbox')))
    target = get_setting(db, 'daily_goal', 10)
    goal_met = len(daily) >= target or (not queue and bool(daily))
    eligible = {
        'first_reply': any(e.action == 'reply' for e in events), 'decisive': len(session) >= 10,
        'streak_3': streak >= 3, 'streak_7': streak >= 7,
        'speed': any(session[i+4].at - session[i].at <= 300 for i in range(max(0, len(session)-4))),
        'zero': not queue and bool(events), 'century': len(events) >= 100}
    newly = []
    if unlock:
        for key, earned in eligible.items():
            if earned and not db.get(Badge, key):
                db.add(Badge(key=key, unlocked_at=time.time())); newly.append(key)
        db.flush()
    badges = [{'key': key, 'name': name, 'description': desc, 'icon': icon, 'unlocked': bool(db.get(Badge, key))} for key, name, desc, icon in BADGES]
    return {'score': sum(e.points for e in session), 'session_processed': len(session), 'streak': streak,
            'daily_processed': len(daily), 'daily_goal': target, 'goal_met': goal_met,
            'progress': 1 if goal_met else min(1, len(daily)/target), 'total': len(events),
            'badges': badges, 'new_badges': newly}
