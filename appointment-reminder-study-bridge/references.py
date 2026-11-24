"""Resolve explicit deictic reminder requests from the immediately prior reply."""
import re
from datetime import datetime, timedelta

REFERENCE = re.compile(r'(その時間|その時|そのとき|そのタイミング|終わったら|終わる時|終わるとき|到时候|到時候|那个时间|那個時間|那时候|那時候|届时|屆時|等你.{0,5}(下课|结束)|结束后)')
NOTIFY = re.compile(r'(提醒我|叫我|喊我|通知我|告诉我|呼んで|知らせて|教えて|起こして|連絡.{0,10}(して|ください|お願い))')
HOUR = r'[0-9一二三四五六七八九十〇零两]{1,3}'
CLOCK = re.compile(rf'(?<!\d)(?:(\d{{1,2}}):(\d{{2}})|({HOUR})[時点](?:({HOUR})分?)?)')
DAY = re.compile(r'(明後日|明后天|后天|明日|明天|今日|今天|本日|昨日|昨天|今夜|今晚)')


def is_reference(text):
    if re.search(r'(その時間を教えて|その時間(?:は|って).{0,4}(?:いつ|何時)|告诉我那个时间(?:是|$)|告诉我几点)', text or ''):
        return False
    if re.search(r'((?:不要|别|不用|不必).{0,6}(?:提醒|叫我|喊我|通知)|呼ばない|知らせない|連絡しない|呼んで.{0,8}(?:なくて|いらない))', text or ''):
        return False
    return bool(REFERENCE.search(text or '') and NOTIFY.search(text or ''))


def number(value):
    if not value:
        return 0
    if value.isdigit():
        return int(value)
    digits = {'〇': 0, '零': 0, '一': 1, '二': 2, '两': 2, '三': 3, '四': 4, '五': 5, '六': 6, '七': 7, '八': 8, '九': 9}
    if '十' in value:
        left, right = value.split('十', 1)
        return (digits.get(left, 1) if left else 1) * 10 + (digits.get(right, 0) if right else 0)
    return digits.get(value, -1)


def resolve(text, history, captured_at, received_at):
    """Return a time only for a fresh, unique prior assistant clock and explicit ask."""
    if not is_reference(text):
        return {'status': 'not_reference'}
    try:
        captured = datetime.fromisoformat(str(captured_at))
        if captured.tzinfo is None:
            raise ValueError('Unscoped time')
        captured = captured.astimezone(received_at.tzinfo)
    except (ValueError, TypeError):
        return {'status': 'ambiguous'}
    if not timedelta(0) <= received_at - captured <= timedelta(hours=2):
        return {'status': 'ambiguous'}
    source = next((str(turn.get('content') or '') for turn in reversed(history or []) if turn.get('role') == 'assistant'), '')
    if source.startswith(('【读音补充', '【词句解析', '【全文解析', '【日语解析')):
        return {'status': 'ambiguous'}
    clocks = list(CLOCK.finditer(source))
    candidates = set()
    for match in clocks:
        hour, minute = (int(match[1]), int(match[2])) if match[1] else (number(match[3]), number(match[4]))
        if source[match.end():].startswith('半'):
            if match[2] or match[4]:
                return {'status': 'ambiguous'}
            minute = 30
        prefix = source[max(0, match.start()-12):match.start()]
        if re.search(r'(午後|下午|晚上|夕方)\s*$', prefix) and 1 <= hour < 12:
            hour += 12
        elif re.search(r'(午前|凌晨)\s*$', prefix) and hour == 12:
            hour = 0
        if not 0 <= hour < 24 or not 0 <= minute < 60:
            return {'status': 'ambiguous'}
        candidates.add((hour, minute))
    if len(candidates) != 1:
        return {'status': 'ambiguous'}
    # Explicit user date overrides the referenced reply's date. Never roll an
    # already elapsed same-day request to tomorrow just to obtain a future time.
    days = DAY.findall(text) or DAY.findall(source)
    if not days and re.search(r'(来週|来月|下周|下个月|[月火水木金土日]曜日|周[一二三四五六日天]|星期[一二三四五六日天])', source + text):
        return {'status': 'ambiguous'}
    offsets = {'明後日': 2, '明后天': 2, '后天': 2, '明日': 1, '明天': 1, '昨日': -1, '昨天': -1}
    day_offsets = {offsets.get(day, 0) for day in days}
    if len(day_offsets) > 1:
        return {'status': 'ambiguous'}
    # Calendar dates require the full extractor; do not silently discard them.
    if re.search(r'\d{1,4}[年/-]\d{1,2}|\d{1,2}月\d{1,2}', source + text):
        return {'status': 'ambiguous'}
    offset = next(iter(day_offsets), 0)
    hour, minute = next(iter(candidates))
    due = received_at.replace(hour=hour, minute=minute, second=0, microsecond=0) + timedelta(days=offset)
    if due <= received_at:
        return {'status': 'elapsed', 'due_at': due, 'reference': source}
    return {'status': 'resolved', 'due_at': due, 'reference': source}
