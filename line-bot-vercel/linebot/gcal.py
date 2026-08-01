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

SCOPES = ['https://www.googleapis.com/auth/calendar.readonly']
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
