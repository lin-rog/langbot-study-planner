"""Deterministic scheduling without conversation memory or SDK dependencies."""
import hashlib
import json
import re
from datetime import datetime, time, timedelta


class Planner:
    def __init__(self, timetable, state=None):
        self.timetable = timetable
        self.state = state if state is not None else {}
        for key in ('plans', 'ack', 'nag', 'delivery'):
            self.state.setdefault(key, {})
        dates = [e['date'] for e in timetable['events']]
        self.first = datetime.fromisoformat(timetable['week7Monday']).date()
        self.last = self.first + timedelta(days=69)
        self.external_days = set()
        if not dates:
            raise ValueError('Empty timetable')

    def active(self, day):
        return self.first <= day <= self.last

    def acknowledge(self, tomorrow, now):
        for day in set(self.state['nag']) | {str(tomorrow)}:
            self.state['ack'][day] = now.isoformat()

    def classes(self, day):
        return sorted((e for e in self.timetable['events'] if e['date'] == str(day)),
                      key=lambda e: e['time'])

    def blocks(self, day):
        return [(e['time'], e['course'], e.get('room', '')) for e in self.classes(day)]

    def overview(self, day):
        lines = [f'{day} 课表与计划：']
        for span, title, room in self.blocks(day):
            lines.append(f'{span} {title} {room}')
        if not self.classes(day):
            lines.append('当天没有课程。')
        plan = self.state['plans'].get(str(day))
        if plan:
            lines.append('个人计划：' + ('不另作安排' if not plan['items'] else ''))
            lines.extend(f"{i['time']} {i['title']}" for i in plan['items'])
        elif str(day) in self.external_days:
            lines.append('已在约定提醒中登记学习安排，由约定提醒负责到点通知。')
        else:
            lines.append('个人计划尚未提交。')
        if str(day) in self.state['ack'] and not plan:
            lines.append('已明确回复，本轮催促已停止；计划仍未提交。')
        busy = [x[0] for x in self.blocks(day)]
        busy += [i['time'] for i in (plan or {}).get('items', [])]
        intervals = sorted((s[:5], s[6:]) for s in busy)
        cursor = '07:00'
        free = []
        for start, end in intervals:
            if start > cursor:
                free.append(f'{cursor}-{start}')
            cursor = max(cursor, end)
        if cursor < '23:00':
            free.append(f'{cursor}-23:00')
        lines.append('可规划时段（07:00–23:00）：' + ('、'.join(free) or '无'))
        return '\n'.join(lines)

    def submit(self, day, text, now):
        if day != now.date() + timedelta(days=1):
            raise ValueError('请在前一天提交次日计划。')
        if not self.active(day):
            raise ValueError('日期超出本课表范围。')
        if len(text) > 4000:
            raise ValueError('计划最多4000字。')
        items = []
        if text.strip() != '不另作安排':
            chunks = re.split(r'[;；\n]+', text.strip())
            for chunk in chunks:
                if not chunk.strip():
                    continue
                match = re.fullmatch(r'\s*(\d{2}:\d{2})-(\d{2}:\d{2})\s+(.{1,200})\s*', chunk)
                if not match:
                    raise ValueError('格式：!规划 18:00-19:00 复习Python；20:00-21:00 做练习')
                start, end, title = match.groups()
                try:
                    time.fromisoformat(start)
                    time.fromisoformat(end)
                except ValueError:
                    raise ValueError('时间必须是有效的HH:MM。') from None
                if start >= end:
                    raise ValueError('结束时间须晚于开始时间，不能跨日。')
                items.append({'time': f'{start}-{end}', 'title': title.strip()})
            if not items or len(items) > 24:
                raise ValueError('请提交1–24个时间段，或“不另作安排”。')
            occupied = [(span, title) for span, title, _ in self.blocks(day)]
            for item in sorted(items, key=lambda i: i['time']):
                span = item['time']
                for other, title in occupied:
                    if span[:5] < other[6:] and other[:5] < span[6:]:
                        raise ValueError(f"{span} 与 {other} {title} 冲突，请调整。")
                occupied.append((span, item['title']))
        self.state['plans'][str(day)] = {'items': items, 'submitted_at': now.isoformat()}
        self.acknowledge(day, now)

    def handle(self, text, now):
        tomorrow = now.date() + timedelta(days=1)
        text = text.strip()
        if text == '!规划帮助':
            return ('!明日课表 / !今日课表\n!规划 HH:MM-HH:MM 事项；HH:MM-HH:MM 事项\n'
                    '!规划 不另作安排\n!规划回应：明确回应并停止今晚催促，但仍待提交\n'
                    '!今晚跳过：停止今晚催促\n!暂停催促 / !恢复催促：长期暂停/恢复\n'
                    '!规划状态 / !规划约定：查看状态/导出明日个人计划的约定命令')
        if text in ('!明日课表', '!今日课表'):
            return self.overview(tomorrow if text == '!明日课表' else now.date())
        if text in ('!暂停催促', '!恢复催促'):
            self.state['paused'] = text == '!暂停催促'
            return '已暂停催促；上课和计划提醒仍有效。' if self.state['paused'] else '已恢复催促。'
        if text in ('!规划回应', '!今晚跳过', '我在规划', '今晚跳过规划', '明天不另作安排'):
            if text == '明天不另作安排':
                try:
                    self.submit(tomorrow, '不另作安排', now)
                except ValueError as exc:
                    return str(exc)
                return '已登记明天不另作安排。'
            self.acknowledge(tomorrow, now)
            return '收到明确回复，当前催促已停止；未提交的计划仍保持待定。'
        if text.startswith('!规划 '):
            try:
                self.submit(tomorrow, text[4:].strip(), now)
            except ValueError as exc:
                return str(exc)
            return '明日计划已保存，今晚不再催促。\n' + self.overview(tomorrow)
        if text == '!规划状态':
            plan = self.state['plans'].get(str(tomorrow)) or str(tomorrow) in self.external_days
            return (f"明日计划：{'已提交' if plan else '未提交'}；"
                    f"催促：{'长期暂停' if self.state.get('paused') else '本轮已停止' if str(tomorrow) in self.state['ack'] else '22:00开始'}。")
        if text == '!规划约定':
            items = self.state['plans'].get(str(tomorrow), {}).get('items', [])
            return ('导出命令仅用于人工复制到约定提醒；启用前请关闭本插件个人计划提醒，避免重复：\n' +
                    '\n'.join(f"!约定 {tomorrow} {i['time'][:5]} {i['title']}" for i in items)) if items else '明日没有可导出的个人计划。'
        return None

    def jobs(self, now, cfg):
        jobs = []
        for day in (now.date(), now.date() + timedelta(days=1)):
            if not self.active(day):
                continue
            things = []
            if cfg.get('class_reminders_enabled', True):
                things += [{'time': e['time'], 'title': e['course'], 'room': e.get('room', ''), 'kind': 'class'} for e in self.classes(day)]
            if cfg.get('plan_reminders_enabled', True):
                things += [dict(i, kind='plan', room='') for i in self.state['plans'].get(str(day), {}).get('items', [])]
            lead = max(0, min(120, int(cfg.get('lead_minutes', 10))))
            for item in things:
                start = datetime.combine(day, time.fromisoformat(item['time'][:5]), now.tzinfo)
                due = start - timedelta(minutes=lead)
                # Never deliver stale reminders once the activity starts.
                if due <= now < start + timedelta(seconds=60 if lead == 0 else 0.001):
                    key = 'event:' + hashlib.sha256(json.dumps([str(day), item], sort_keys=True).encode()).hexdigest()[:24]
                    if key not in self.state['delivery']:
                        jobs.append({'id': key, 'kind': item['kind'], 'date': str(day), 'item': item,
                                     'text': f"{item['time']} {item['title']} {item['room']}，准备开始了。"})
        tomorrow = now.date() + timedelta(days=1)
        daykey = str(tomorrow)
        pending = sorted(d for d in self.state['nag']
                         if d not in self.state['ack'] and d not in self.state['plans'] and d not in self.external_days)
        if pending:
            daykey = pending[0]
        eligible = bool(pending) or (self.active(tomorrow) and now.hour >= 22
                                    and daykey not in self.state['plans'] and daykey not in self.state['ack'] and daykey not in self.external_days)
        if cfg.get('nag_enabled', True) and eligible and not self.state.get('paused'):
            nag = self.state['nag'].get(daykey, {})
            last = datetime.fromisoformat(nag['last']) if nag.get('last') else None
            if last is None or (now - last).total_seconds() >= 120:
                first = datetime.fromisoformat(nag['first']) if nag.get('first') else now
                negative = (now - first).total_seconds() >= 600
                count = nag.get('count', 0)
                jobs.append({'id': f'nag:{daykey}:{count}', 'kind': 'nag', 'date': daykey,
                             'signal': 'next_day_plan_missing', 'attempt': count + 1,
                             'waiting_seconds': max(0, int((now - first).total_seconds())),
                             'tone': 'impatient_teasing' if negative else 'gentle'})
        return jobs

    def reserve(self, job, now):
        self.state['delivery'][job['id']] = {'at': now.isoformat(), 'status': 'reserved'}
        if job['kind'] == 'nag':
            nag = self.state['nag'].setdefault(job['date'], {'first': now.isoformat(), 'count': 0})
            nag.update(last=now.isoformat(), count=nag['count'] + 1)

    def prune(self, now):
        cutoff = str(now.date() - timedelta(days=14))
        pending = {d for d in self.state['nag'] if d not in self.state['ack'] and d not in self.state['plans']}
        for key in ('plans', 'ack', 'nag'):
            self.state[key] = {d: v for d, v in self.state[key].items()
                               if d >= cutoff or (key == 'nag' and d in pending)}
        self.state['delivery'] = {k: v for k, v in self.state['delivery'].items() if v['at'][:10] >= cutoff}
        self.state['wording_retry_after'] = {k: v for k, v in self.state.get('wording_retry_after', {}).items()
                                           if datetime.fromisoformat(v) > now}
