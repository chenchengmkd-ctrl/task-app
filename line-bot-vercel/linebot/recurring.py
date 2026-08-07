"""定期タスク（テンプレート）：登録・次回日付計算・リマインド。Code.gs の該当セクションに相当。
定期タスクは「リマインドのみ」方式（ボードへの自動追加はしない）。
"""
import calendar
from datetime import datetime, timedelta
from urllib.parse import quote

from . import config
from .supabase_client import (get_supabase, post_supabase, patch_supabase, delete_supabase,
                              get_state, set_state, list_state_keys, delete_state)

WEEKDAY_JP = ['日', '月', '火', '水', '木', '金', '土']


def get_recurring():
    """定期タスクを取得。"""
    rows = get_supabase('recurring', 'select=id,title,recurrence,weekday,monthday,next_date,remind_time')
    out = []
    for r in rows:
        out.append({
            'id': r['id'], 'title': str(r['title']), 'recurrence': str(r.get('recurrence') or ''),
            'weekday': r.get('weekday'), 'monthday': r.get('monthday'),
            'next': config.parse_date(r.get('next_date')), 'remindTime': r.get('remind_time') or '',
        })
    return out


def rec_desc(r):
    """表示用ラベル（毎週水曜／毎月15日／毎月末日など）。"""
    if r.get('recurrence') == 'weekly' and r.get('weekday') not in (None, ''):
        return f"毎週{WEEKDAY_JP[int(r['weekday'])]}曜"
    if r.get('recurrence') == 'monthly' and r.get('monthday') not in (None, ''):
        return '毎月末日' if r['monthday'] == 'last' else f"毎月{r['monthday']}日"
    if r.get('recurrence') == 'daily':
        return '毎日'
    return r.get('recurrence') or ''


def recurring_summary_message(rec):
    """一覧コマンド用：登録済み定期タスクの別メッセージ本文を組み立てる。"""
    lines = []
    for r in rec:
        next_part = f"・次回 {config.jp2(r['next'])}" if r['next'] else ''
        time_part = r['remindTime'] or f'{config.REMIND_HOUR:02d}:{config.REMIND_MINUTE:02d}'
        lines.append(f"・{r['title']}（{rec_desc(r)}{next_part}・{time_part}）")
    return f'🔁 登録されている定期タスク（{len(rec)}）\n' + '\n'.join(lines)


def recurring_list_message():
    """「定期タスク」コマンド用：登録済みの定期タスクだけを表示する（通常のタスク一覧とは分ける）。"""
    rec = get_recurring()
    if not rec:
        return '🔁 登録されている定期タスクはありません。'
    return recurring_summary_message(rec)


def monthday_for(y, m, monthday):
    last = calendar.monthrange(y, m)[1]
    if monthday == 'last':
        return last
    return min(int(monthday), last)


def first_weekly(start_iso, weekday):
    """開始日以降で最初に weekday になる日。"""
    y, mo, d = (int(x) for x in start_iso.split('-')[:3])
    dt = datetime(y, mo, d, tzinfo=config.JST)
    py_dow = dt.weekday()          # 0=月...6=日
    js_dow = (py_dow + 1) % 7      # 0=日...6=土 に変換
    delta = (int(weekday) - js_dow + 7) % 7
    return config.iso_of_date(dt + timedelta(days=delta))


def first_monthly(start_iso, monthday):
    """開始日以降で最初に monthday になる日。"""
    y, mo, d = (int(x) for x in start_iso.split('-')[:3])
    s = datetime(y, mo, d, tzinfo=config.JST)
    yy, mm = y, mo
    cand = datetime(yy, mm, monthday_for(yy, mm, monthday), tzinfo=config.JST)
    if cand < s:
        mm += 1
        if mm > 12:
            mm = 1
            yy += 1
        cand = datetime(yy, mm, monthday_for(yy, mm, monthday), tzinfo=config.JST)
    return config.iso_of_date(cand)


def next_recur_date(current_iso, r):
    """現在の予定日から次の周期の予定日を計算する。"""
    y, mo, d = (int(x) for x in current_iso.split('-')[:3])
    base = datetime(y, mo, d, tzinfo=config.JST)
    if r.get('recurrence') == 'weekly':
        return config.iso_of_date(base + timedelta(days=7))
    if r.get('recurrence') == 'monthly':
        md = r.get('monthday') if r.get('monthday') not in (None, '') else base.day
        yy, mm = base.year, base.month + 1
        if mm > 12:
            mm = 1
            yy += 1
        return config.iso_of_date(datetime(yy, mm, monthday_for(yy, mm, md), tzinfo=config.JST))
    return config.iso_of_date(base + timedelta(days=1))


def _defer_past_events(date_iso, hhmm):
    """その時刻がカレンダーの予定と重なっていたら、空く時刻（HH:MM）を返す。重なっていなければNone。"""
    from . import gcal
    if not gcal.conflict_at(date_iso, hhmm):
        return None
    later = gcal.suggest_time(date_iso, after_hhmm=hhmm)
    # ずらす先が見つからなければ（その日ずっと予定が詰まっている等）、そのまま通知する
    return later if later and later > hhmm else None


def check_recurring_reminders(push_text_fn, get_users_fn):
    """定期タスクのリマインド時刻（未設定なら毎朝の通知時刻）が到来したら、別メッセージで通知し次回予定日へ進める。"""
    today_str = config.today_iso()
    now = config.now_jst()
    items = [r for r in get_recurring() if r['next'] and config.iso_of_date(r['next']) <= today_str]
    if not items:
        return

    prop_key = f'REC_REMINDED_{today_str}'
    reminded = get_state(prop_key) or []
    defer_key = f'REC_DEFERRED_{today_str}'
    deferred = get_state(defer_key) or {}
    defer_changed = False

    fire, overdue = [], []
    for r in items:
        if r['id'] in reminded:
            continue
        if config.iso_of_date(r['next']) < today_str:
            # 予定日を過ぎたまま溜まっている分は、時刻を待たずにすぐ通知して日付を進める
            fire.append(r)
            overdue.append(r)
            reminded.append(r['id'])
            continue
        hhmm = deferred.get(r['id']) or r['remindTime'] or f'{config.REMIND_HOUR:02d}:{config.REMIND_MINUTE:02d}'
        hh, mm = hhmm.split(':')
        target = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
        diff_min = (target - now).total_seconds() / 60
        # 今日ぶんは、予定時刻を過ぎた直後（チェック間隔ぶんの幅）で1回だけ捕まえる
        if -config.TIME_CHECK_INTERVAL < diff_min <= 0:
            # カレンダーに予定が入っている時間帯なら、その予定が終わるまで通知を後ろにずらす
            later = _defer_past_events(today_str, hhmm)
            if later:
                deferred[r['id']] = later
                defer_changed = True
                continue
            fire.append(r)
            reminded.append(r['id'])

    if defer_changed:
        set_state(defer_key, deferred)
    if not fire:
        return

    msg = '🔁 今日の定期タスク\n' + '\n'.join(f"・{r['title']}" for r in fire)
    if any(r['id'] in deferred for r in fire):
        msg += '\n\n（カレンダーの予定と重なっていたため、空く時間まで通知をずらしました）'
    if overdue:
        msg += f'\n\n（うち{len(overdue)}件は予定日を過ぎていたぶんです。次回以降は設定時刻にお知らせします）'
    for uid in get_users_fn():
        push_text_fn(uid, msg)
    set_state(prop_key, reminded)

    # 次回予定日へ進める（溜まっていた場合は今日以前を追い越すまで進める）
    for r in fire:
        n = config.iso_of_date(r['next'])
        n = next_recur_date(n, r)
        while n <= today_str:
            n = next_recur_date(n, r)
        patch_supabase('recurring', f"id=eq.{quote(r['id'])}", {'next_date': n})

    _cleanup_old_rec_reminder_props(today_str)


def _cleanup_old_rec_reminder_props(today_str):
    for k in list_state_keys('REC_REMINDED_'):
        if k != f'REC_REMINDED_{today_str}':
            delete_state(k)


def handle_create_recurring_task(input_args, intro):
    """定期タスク（テンプレート）の新規登録を実行。AIツール create_recurring_task から呼ばれる。"""
    a = input_args or {}
    title = str(a.get('title') or '').strip()
    recurrence = str(a.get('recurrence') or '').strip()
    weekday = a.get('weekday')
    monthday = a.get('monthday')
    remind_time = str(a.get('remind_time') or '').strip()
    start_date = str(a.get('start_date') or '').strip() or config.today_iso()

    if not title or recurrence not in ('daily', 'weekly', 'monthly'):
        return '⚠️ 定期タスクの登録内容を理解できませんでした。'
    if recurrence == 'weekly' and weekday in (None, ''):
        return '⚠️ 毎週の場合は曜日も教えてください（例：毎週月曜）。'
    if recurrence == 'monthly' and monthday in (None, ''):
        return '⚠️ 毎月の場合は日にちも教えてください（例：毎月15日、毎月末日）。'

    if recurrence == 'weekly':
        next_date = first_weekly(start_date, weekday)
    elif recurrence == 'monthly':
        next_date = first_monthly(start_date, monthday)
    else:
        next_date = start_date

    ok = post_supabase('recurring', [{
        'id': config.new_id(), 'title': title, 'recurrence': recurrence,
        'weekday': int(weekday) if recurrence == 'weekly' else None,
        'monthday': str(monthday) if recurrence == 'monthly' else None,
        'next_date': next_date, 'remind_time': remind_time or None,
        'priority': 'low', 'estimate': '', 'created_at': config.now_iso(),
    }])
    if not ok:
        return '⚠️ 定期タスクの登録に失敗しました。時間をおいて再度お試しください。'

    desc = rec_desc({'recurrence': recurrence, 'weekday': weekday, 'monthday': monthday})
    if remind_time:
        time_label = f'{remind_time}にリマインド'
    else:
        time_label = f'毎朝{config.REMIND_HOUR:02d}:{config.REMIND_MINUTE:02d}の通知と同じタイミングでリマインド'
    return f'🔁「{title}」を定期タスクとして登録しました\n{desc}・{time_label}\n次回：{config.jp(next_date)}'


def find_recurring_by_title(title):
    return get_supabase('recurring', f'title=eq.{quote(title)}&select=id,title,recurrence,weekday,monthday,next_date,remind_time')


def handle_update_recurring_task(input_args, intro):
    """定期タスクの周期・曜日・日にち・リマインド時刻・タイトルを変更する。AIツール update_recurring_task から呼ばれる。"""
    a = input_args or {}
    title = str(a.get('task_title') or '').strip()
    if not title:
        return '⚠️ 対象の定期タスクを理解できませんでした。'

    matches = find_recurring_by_title(title)
    if not matches:
        return f'⚠️「{title}」という定期タスクが見つかりませんでした。「定期タスク」で確認してください。'
    if len(matches) > 1:
        return f'⚠️「{title}」に一致する定期タスクが複数あります。アプリ側で確認・変更してください。'
    r = matches[0]

    new_title = str(a.get('new_title') or '').strip()
    recurrence = str(a.get('recurrence') or '').strip() or r['recurrence']
    weekday = a.get('weekday')
    monthday = a.get('monthday')
    remind_time_raw = a.get('remind_time')

    body = {}
    if new_title:
        body['title'] = new_title
    if remind_time_raw is not None:
        rt = str(remind_time_raw).strip()
        body['remind_time'] = None if rt in ('なし', '未設定', 'none', '') else rt

    schedule_changed = (
        recurrence != r['recurrence']
        or (weekday not in (None, '') and int(weekday) != (r.get('weekday') if r.get('weekday') is not None else -1))
        or (monthday not in (None, '') and str(monthday) != str(r.get('monthday') or ''))
    )
    if schedule_changed:
        if recurrence == 'weekly':
            wd = weekday if weekday not in (None, '') else r.get('weekday')
            if wd is None:
                return '⚠️ 毎週の場合は曜日も教えてください（例：毎週月曜）。'
            body['weekday'], body['monthday'] = int(wd), None
            body['next_date'] = first_weekly(config.today_iso(), wd)
        elif recurrence == 'monthly':
            md = monthday if monthday not in (None, '') else r.get('monthday')
            if md in (None, ''):
                return '⚠️ 毎月の場合は日にちも教えてください（例：毎月15日、毎月末日）。'
            body['weekday'], body['monthday'] = None, str(md)
            body['next_date'] = first_monthly(config.today_iso(), md)
        else:
            body['weekday'], body['monthday'] = None, None
            body['next_date'] = config.today_iso()
        body['recurrence'] = recurrence

    if not body:
        return '⚠️ 変更内容が分かりませんでした。'

    ok = patch_supabase('recurring', f"id=eq.{quote(r['id'])}", body)
    if ok is None:
        return f'⚠️「{title}」の変更に失敗しました。'

    desc = rec_desc({'recurrence': body.get('recurrence', r['recurrence']),
                     'weekday': body.get('weekday', r.get('weekday')),
                     'monthday': body.get('monthday', r.get('monthday'))})
    remind_time = body.get('remind_time', r.get('remind_time'))
    time_label = f'{remind_time}にリマインド' if remind_time else f'毎朝{config.REMIND_HOUR:02d}:{config.REMIND_MINUTE:02d}の通知と同じタイミングでリマインド'
    label = new_title or title
    msg = f'✅「{label}」を更新しました\n{desc}・{time_label}'
    if 'next_date' in body:
        msg += f'\n次回：{config.jp(body["next_date"])}'
    return msg


def handle_delete_recurring_task(input_args, intro):
    """定期タスクを削除する。AIツール delete_recurring_task から呼ばれる。"""
    a = input_args or {}
    title = str(a.get('task_title') or '').strip()
    if not title:
        return '⚠️ 削除対象の定期タスクを理解できませんでした。'

    matches = find_recurring_by_title(title)
    if not matches:
        return f'⚠️「{title}」という定期タスクが見つかりませんでした。「定期タスク」で確認してください。'
    if len(matches) > 1:
        return f'⚠️「{title}」に一致する定期タスクが複数あります。アプリ側で確認・削除してください。'

    ok = delete_supabase('recurring', f"id=eq.{quote(matches[0]['id'])}")
    if not ok:
        return f'⚠️「{title}」の削除に失敗しました。'

    return f'🗑️「{title}」の定期タスクを削除しました。'
