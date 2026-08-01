"""Googleカレンダー連携（読み取り専用）。

サービスアカウント方式：ユーザーが自分のカレンダーをサービスアカウントのメールアドレスに
「閲覧権限」で共有しておくと、こちらから予定を読める。OAuthの同意画面やトークンの更新が
不要なので、非エンジニアでも1回の設定で済み、あとから切れることもない。

未設定のときは常に「連携なし」として静かに無効化する（他の機能に影響を出さない）。
"""
import json
from datetime import timedelta
from urllib.parse import quote

import requests

from . import config

# 読み書き両方のスコープ。実際に書き込めるかどうかは、ユーザーがカレンダー共有で
# 「予定の変更」権限を渡しているかで決まる（閲覧のみのままなら書き込みは403になる）。
SCOPES = ['https://www.googleapis.com/auth/calendar']
_API = 'https://www.googleapis.com/calendar/v3'


def is_enabled():
    return bool(config.GOOGLE_SERVICE_ACCOUNT_JSON and config.GOOGLE_CALENDAR_ID)


def _access_token():
    """サービスアカウントのJSONキーからアクセストークンを得る。失敗時はNone。"""
    if not config.GOOGLE_SERVICE_ACCOUNT_JSON:
        return None
    try:
        from google.oauth2 import service_account
        from google.auth.transport.requests import Request
        info = json.loads(config.GOOGLE_SERVICE_ACCOUNT_JSON)
        creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
        creds.refresh(Request())
        return creds.token
    except Exception as e:
        print('gcal token error:', e)
        return None


def _hhmm_to_min(hhmm):
    h, m = str(hhmm)[:5].split(':')
    return int(h) * 60 + int(m)


def _min_to_hhmm(mins):
    return f'{mins // 60:02d}:{mins % 60:02d}'


def get_events(date_iso):
    """その日の予定を取得。→ [{'summary','start_min','end_min','all_day'}]（連携なし・失敗時は []）"""
    if not is_enabled():
        return []
    token = _access_token()
    if not token:
        return []

    day = config.parse_date(date_iso)
    if not day:
        return []
    time_min = quote(day.strftime('%Y-%m-%dT00:00:00+09:00'), safe='')
    time_max = quote((day + timedelta(days=1)).strftime('%Y-%m-%dT00:00:00+09:00'), safe='')
    url = (f'{_API}/calendars/{quote(config.GOOGLE_CALENDAR_ID, safe="")}/events'
           f'?timeMin={time_min}&timeMax={time_max}&singleEvents=true&orderBy=startTime&maxResults=50')
    try:
        res = requests.get(url, headers={'Authorization': f'Bearer {token}'}, timeout=20)
    except requests.RequestException as e:
        print('gcal request error:', e)
        return []
    if res.status_code != 200:
        print('gcal error', res.status_code, res.text[:300])
        return []

    out = []
    for ev in res.json().get('items', []):
        if ev.get('status') == 'cancelled':
            continue
        # 「予定なし（透明）」の予定は空き時間を埋めない扱いにする
        if ev.get('transparency') == 'transparent':
            continue
        start, end = ev.get('start') or {}, ev.get('end') or {}
        if start.get('date'):          # 終日予定
            out.append({'summary': ev.get('summary') or '(無題)', 'start_min': 0, 'end_min': 24 * 60,
                        'all_day': True})
            continue
        s, e = start.get('dateTime'), end.get('dateTime')
        if not s or not e:
            continue
        sd, ed = config.parse_timestamp(s), config.parse_timestamp(e)
        if not sd or not ed:
            continue
        s_min = sd.hour * 60 + sd.minute
        e_min = ed.hour * 60 + ed.minute
        if config.iso_of_date(ed) != date_iso:      # 日をまたぐ予定はその日の終わりまで
            e_min = 24 * 60
        if e_min <= s_min:
            e_min = min(s_min + 30, 24 * 60)
        out.append({'summary': ev.get('summary') or '(無題)', 'start_min': s_min, 'end_min': e_min,
                    'all_day': False})
    return out


def get_events_with_id(date_iso):
    """予定をイベントIDつきで取得（変更・削除の対象を選ぶ用）。"""
    if not is_enabled():
        return []
    token = _access_token()
    if not token:
        return []
    day = config.parse_date(date_iso)
    if not day:
        return []
    time_min = quote(day.strftime('%Y-%m-%dT00:00:00+09:00'), safe='')
    time_max = quote((day + timedelta(days=1)).strftime('%Y-%m-%dT00:00:00+09:00'), safe='')
    url = (f'{_API}/calendars/{quote(config.GOOGLE_CALENDAR_ID, safe="")}/events'
           f'?timeMin={time_min}&timeMax={time_max}&singleEvents=true&orderBy=startTime&maxResults=50')
    try:
        res = requests.get(url, headers={'Authorization': f'Bearer {token}'}, timeout=20)
    except requests.RequestException as e:
        print('gcal request error:', e)
        return []
    if res.status_code != 200:
        print('gcal error', res.status_code, res.text[:300])
        return []
    out = []
    for ev in res.json().get('items', []):
        if ev.get('status') == 'cancelled':
            continue
        start, end = ev.get('start') or {}, ev.get('end') or {}
        out.append({
            'id': ev.get('id'), 'summary': ev.get('summary') or '(無題)',
            'start': start.get('dateTime') or start.get('date'),
            'end': end.get('dateTime') or end.get('date'),
            'all_day': bool(start.get('date')),
        })
    return out


def find_event(date_iso, keyword):
    """その日の予定からキーワードに合うものを探す。→ (見つかった予定, 候補一覧)"""
    events = get_events_with_id(date_iso)
    kw = str(keyword or '').strip()
    if not kw:
        return None, events
    exact = [e for e in events if e['summary'] == kw]
    if len(exact) == 1:
        return exact[0], events
    partial = [e for e in events if kw in e['summary'] or e['summary'] in kw]
    if len(partial) == 1:
        return partial[0], events
    return None, (partial or events)


def event_label(ev):
    """予定を「14:00-16:00 仕込み」の形で表す。"""
    if ev.get('all_day'):
        return f"終日 {ev['summary']}"
    s, e = config.parse_timestamp(ev.get('start')), config.parse_timestamp(ev.get('end'))
    if not s:
        return ev['summary']
    span = f'{s.hour:02d}:{s.minute:02d}'
    if e:
        span += f'-{e.hour:02d}:{e.minute:02d}'
    return f"{span} {ev['summary']}"


def _write(method, path, body=None):
    """カレンダーへの書き込み共通処理。→ (成功したか, メッセージ)"""
    if not is_enabled():
        return False, 'カレンダーが連携されていません。'
    token = _access_token()
    if not token:
        return False, 'カレンダーの認証に失敗しました。'
    url = f'{_API}/calendars/{quote(config.GOOGLE_CALENDAR_ID, safe="")}{path}'
    try:
        res = requests.request(
            method, url,
            headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
            data=json.dumps(body) if body is not None else None, timeout=20,
        )
    except requests.RequestException as e:
        return False, f'通信に失敗しました（{e}）'
    if res.status_code in (200, 201, 204):
        return True, ''
    if res.status_code == 403:
        return False, ('カレンダーへの書き込み権限がありません。\n'
                       'Googleカレンダーの共有設定で、mytask-bot… の権限を「予定の変更」に変えてください。')
    print('gcal write error', res.status_code, res.text[:300])
    return False, f'カレンダーの更新に失敗しました（{res.status_code}）'


def _dt(date_iso, hhmm):
    return {'dateTime': f'{date_iso}T{hhmm}:00+09:00', 'timeZone': 'Asia/Tokyo'}


def create_event(title, date_iso, start_hhmm='', end_hhmm=''):
    """予定を追加する。時刻を省略すると終日予定になる。→ (成功したか, メッセージ)"""
    if start_hhmm:
        if not end_hhmm:
            end_min = min(_hhmm_to_min(start_hhmm) + 60, 24 * 60 - 1)
            end_hhmm = _min_to_hhmm(end_min)
        body = {'summary': title, 'start': _dt(date_iso, start_hhmm), 'end': _dt(date_iso, end_hhmm)}
    else:
        day = config.parse_date(date_iso)
        body = {'summary': title, 'start': {'date': date_iso},
                'end': {'date': config.iso_of_date(day + timedelta(days=1))}}
    return _write('POST', '/events', body)


def update_event(event_id, date_iso, start_hhmm='', end_hhmm='', title=''):
    """予定を書き換える（渡した項目だけ変更）。→ (成功したか, メッセージ)"""
    body = {}
    if title:
        body['summary'] = title
    if start_hhmm:
        if not end_hhmm:
            end_hhmm = _min_to_hhmm(min(_hhmm_to_min(start_hhmm) + 60, 24 * 60 - 1))
        body['start'] = _dt(date_iso, start_hhmm)
        body['end'] = _dt(date_iso, end_hhmm)
    elif end_hhmm:
        body['end'] = _dt(date_iso, end_hhmm)
    if not body:
        return False, '変更する内容がありません。'
    return _write('PATCH', f'/events/{quote(event_id, safe="")}', body)


def delete_event(event_id):
    return _write('DELETE', f'/events/{quote(event_id, safe="")}')


def busy_blocks(date_iso, events=None):
    """予定の時間帯を重なりごとにまとめる。→ [(開始分, 終了分)]"""
    evs = [e for e in (get_events(date_iso) if events is None else events) if not e['all_day']]
    blocks = sorted(((e['start_min'], e['end_min']) for e in evs))
    merged = []
    for s, e in blocks:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def free_slots(date_iso, events=None, from_min=None):
    """予定のあいだの空き時間。→ [(開始分, 終了分)]（config.DAY_START〜DAY_END の範囲内）"""
    start = max(_hhmm_to_min(config.DAY_START), from_min if from_min is not None else 0)
    end = _hhmm_to_min(config.DAY_END)
    slots = []
    cursor = start
    for s, e in busy_blocks(date_iso, events):
        if e <= cursor:
            continue
        if s > cursor:
            slots.append((cursor, min(s, end)))
        cursor = max(cursor, e)
        if cursor >= end:
            break
    if cursor < end:
        slots.append((cursor, end))
    return [(s, e) for s, e in slots if e - s >= config.MIN_FREE_MINUTES and s < end]


def _hours_label(minutes):
    h, m = minutes // 60, minutes % 60
    if h and m:
        return f'{h}時間{m}分'
    if h:
        return f'{h}時間'
    return f'{m}分'


def free_summary(date_iso, from_now=False):
    """空き時間の要約テキスト。連携していない・空きが無い場合はNone。"""
    if not is_enabled():
        return None
    events = get_events(date_iso)
    from_min = None
    if from_now:
        now = config.now_jst()
        if config.iso_of_date(now) == date_iso:
            from_min = now.hour * 60 + now.minute
    slots = free_slots(date_iso, events, from_min)
    if not slots:
        return '🗓 カレンダー上、空き時間はありません。'
    total = sum(e - s for s, e in slots)
    parts = '、'.join(f'{_min_to_hhmm(s)}-{_min_to_hhmm(e)}' for s, e in slots[:4])
    msg = f'🗓 空き時間：{parts}（合計{_hours_label(total)}）'
    fit = total // config.TASK_SLOT_MINUTES
    if fit:
        msg += f'\n　→ 1件1時間で見ると、およそ{fit}件ぶん'
    return msg


def conflict_at(date_iso, hhmm):
    """その時刻に入っている予定の名前を返す（無ければNone）。"""
    if not is_enabled():
        return None
    target = _hhmm_to_min(hhmm)
    for e in get_events(date_iso):
        if e['all_day']:
            continue
        if e['start_min'] <= target < e['end_min']:
            return e['summary']
    return None


def suggest_time(date_iso, duration_min=60, after_hhmm=None):
    """空いている時間帯の先頭を提案する（after_hhmm を渡すとそれ以降で探す。無ければNone）。"""
    from_min = _hhmm_to_min(after_hhmm) if after_hhmm else None
    for s, e in free_slots(date_iso, from_min=from_min):
        if e - s >= min(duration_min, config.MIN_FREE_MINUTES):
            return _min_to_hhmm(s)
    return None


def schedule_text(date_iso, header=None):
    """予定と空き時間を並べたテキスト（AIコーチへの説明や「カレンダー」コマンド用）。"""
    if not is_enabled():
        return None
    events = get_events(date_iso)
    d = config.parse_date(date_iso)
    lines = [header or f'🗓 {config.jp2(d)}の予定']
    if not events:
        lines.append('・予定なし')
    for e in events:
        if e['all_day']:
            lines.append(f"・終日：{e['summary']}")
        else:
            lines.append(f"・{_min_to_hhmm(e['start_min'])}-{_min_to_hhmm(e['end_min'])} {e['summary']}")
    fs = free_summary(date_iso)
    if fs:
        lines.append('')
        lines.append(fs)
    return '\n'.join(lines)


def calendar_command():
    """「カレンダー」コマンド：今日と明日の予定・空き時間を返す。"""
    if not is_enabled():
        return ('🗓 Googleカレンダーはまだ連携されていません。\n'
                'SETUP_GOOGLE_CALENDAR.md の手順で設定すると、予定を踏まえた提案ができるようになります。')
    today = config.today_iso()
    tomorrow = config.iso_of_date(config.now_jst() + timedelta(days=1))
    t1 = schedule_text(today, '🗓 今日の予定')
    t2 = schedule_text(tomorrow, '🗓 明日の予定')
    if t1 is None:
        return '⚠️ カレンダーの読み取りに失敗しました。共有設定とカレンダーIDを確認してください。'
    return t1 + '\n\n' + t2
