"""Calendar-day boundaries for one-off study appointments."""
from datetime import datetime, time, timedelta
import re

STUDY = re.compile(r'(复习|预习|学习|练习|作业|刷题|背单词|备考|看书|读书|阅读|自习|勉強|復習|予習|読書|本を読む)')


def due_time(item, now):
    try:
        due = datetime.fromisoformat(str(item.get('due_at')))
        return due.astimezone(now.tzinfo) if due.tzinfo is not None else None
    except (TypeError, ValueError):
        return None


def is_study(item):
    return item.get('study_plan') is True or (
        item.get('source') in ('command', 'natural') and bool(STUDY.search(str(item.get('summary') or ''))))


def expired_study(item, now):
    due = due_time(item, now)
    return bool(is_study(item) and due and due.date() < now.date())


def expire_studies(reminders, now):
    """Keep dated history while removing expired work from the active queue."""
    changed = False
    for item in reminders:
        if not is_study(item):
            continue
        due = due_time(item, now)
        if due is None:
            continue
        values = {'study_plan': True, 'study_date': due.date().isoformat(),
                  'valid_until': datetime.combine(due.date() + timedelta(days=1), time(), now.tzinfo).isoformat()}
        for key, value in values.items():
            if item.get(key) != value:
                item[key] = value
                changed = True
        if item.get('status') == 'pending' and due.date() < now.date():
            item.update(status='expired', expired_at=now.isoformat(), expiration_reason='study_day_ended')
            item.pop('next_attempt_at', None)
            changed = True
    return changed


def active_appointments(reminders, now):
    return [dict(item) for item in reminders
            if item.get('status') in ('pending', 'delivered') and not expired_study(item, now)]
