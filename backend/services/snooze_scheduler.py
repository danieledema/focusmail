from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

def snooze_time(preset, timezone='Europe/London', now=None):
    zone = ZoneInfo(timezone)
    now = now.astimezone(zone) if now else datetime.now(zone)
    if preset == 'hour':
        return now.timestamp() + 3600
    if preset == 'someday':
        return (now + timedelta(days=30)).timestamp()
    if preset == 'today':
        target = now.replace(hour=17, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
    elif preset == 'tomorrow':
        target = (now + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
    else:
        weekday = 5 if preset == 'weekend' else 0
        days = (weekday - now.weekday()) % 7
        target = (now + timedelta(days=days)).replace(hour=9, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=7)
    return target.timestamp()
