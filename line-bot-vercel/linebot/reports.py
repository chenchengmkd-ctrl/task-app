"""日次・週次レポート。完了と削除を分けて数え、進み具合・ペース・期限切れ・リスケ候補まで出す。"""
from datetime import timedelta

from . import config
from .supabase_client import get_supabase, set_state

WEEKDAY_JP = ['月', '火', '水', '木', '金', '土', '日']


def _short(title, n=30):
    t = str(title or '').replace('\n', ' ').strip()
    return t if len(t) <= n else t[:n] + '…'


def _count(params):
    """件数だけ数える（本文は取らない）。"""
    return len(get_supabase('tasks', params + '&select=id'))


def _completed_between(since_iso, until_iso=None):
    """完了（削除されていないもの）を期間で取得。until_isoは含まない上限。"""
    q = f'done=eq.true&deleted=eq.false&updated_at=gte.{since_iso}&select=title,updated_at&order=updated_at.desc'
    if until_iso:
        q += f'&updated_at=lt.{until_iso}'
    return get_supabase('tasks', q)


def _deleted_since(since_iso):
    return _count(f'deleted=eq.true&updated_at=gte.{since_iso}')


def _open_tasks():
    return get_supabase('tasks', 'done=eq.false&deleted=eq.false&select=id,title,due,due_time')


def _overdue_and_today(open_tasks, today_str):
    """未完了のうち「期限切れ」と「今日が期限」を分けて返す。"""
    overdue, due_today = [], []
    for t in open_tasks:
        d = t.get('due')
        if not d:
            continue
        if d < today_str:
            overdue.append(t)
        elif d == today_str:
            due_today.append(t)
    overdue.sort(key=lambda t: t['due'])
    return overdue, due_today


def _numbered_block(tasks, start_num, user_id, store):
    """タスクに番号を振って行を作り、番号→タスクの対応をstoreに足す。"""
    lines = []
    n = start_num
    for t in tasks:
        time_part = f" {str(t['due_time'])[:5]}" if t.get('due_time') else ''
        due_part = f"（{config.jp(t['due'])}{time_part}）" if t.get('due') else ''
        lines.append(f"{n}. {_short(t['title'])}{due_part}")
        store.append({'num': n, 'id': t['id'], 'title': t['title']})
        n += 1
    return lines, n


def build_daily_report(user_id=None):
    """今日の進み具合をまとめる。番号を振るので、そのまま「1明日」などでリスケできる。"""
    now = config.now_jst()
    today_str = config.today_iso()
    today_start = f'{today_str}T00:00:00+09:00'

    done_today = _completed_between(today_start)
    deleted_today = _deleted_since(today_start)
    open_tasks = _open_tasks()
    overdue, due_today = _overdue_and_today(open_tasks, today_str)

    # 今日が期限のもののうち、何件片付いたか（完了済みで期限が今日のもの）
    done_due_today = _count(f'done=eq.true&deleted=eq.false&due=eq.{today_str}')
    total_due_today = done_due_today + len(due_today)

    since7 = (now - timedelta(days=7)).strftime('%Y-%m-%dT%H:%M:%S+09:00')
    done_7d = len(_completed_between(since7))

    msg = f'📊 日次レポート（{now.month}/{now.day} {WEEKDAY_JP[now.weekday()]}）\n'

    msg += f'\n✅ 今日完了：{len(done_today)}件'
    if done_today:
        msg += '\n' + '\n'.join(f'・{_short(t["title"])}' for t in done_today[:10])
        if len(done_today) > 10:
            msg += f'\n…ほか{len(done_today) - 10}件'

    if total_due_today:
        msg += f'\n\n📅 今日が期限：{total_due_today}件中 {done_due_today}件完了'
    else:
        msg += '\n\n📅 今日が期限のタスクはありません'

    numbered = []
    n = 1
    if due_today:
        lines, n = _numbered_block(due_today, n, user_id, numbered)
        msg += '\n⚠️ 今日が期限で未完了\n' + '\n'.join(lines)
    if overdue:
        lines, n = _numbered_block(overdue[:5], n, user_id, numbered)
        msg += f'\n\n🔴 期限切れ（{len(overdue)}件）'
        msg += '\n' + '\n'.join(lines)
        if len(overdue) > 5:
            msg += f'\n…ほか{len(overdue) - 5}件'

    if numbered:
        msg += '\n\n→ 番号でリスケできます（例：1明日／2を7/30）'
        msg += '\n→ 完了・削除も番号でOK（例：1完了／2削除）'
        if user_id:
            set_state(f'LAST_LIST_{user_id}', {'items': numbered, 'createdAt': config.now_ms()})

    msg += f'\n\n📌 未完了：{len(open_tasks)}件'
    msg += f'\n📈 直近7日の完了：{done_7d}件（1日あたり{done_7d / 7:.1f}件）'
    if deleted_today:
        msg += f'\n🗑 今日削除：{deleted_today}件（完了には数えていません）'
    return msg


def build_weekly_report(user_id=None):
    """先週1週間の完了実績・ペース・積み残しをまとめる。"""
    now = config.now_jst()
    since = (now - timedelta(days=7)).strftime('%Y-%m-%dT%H:%M:%S+09:00')
    prev_since = (now - timedelta(days=14)).strftime('%Y-%m-%dT%H:%M:%S+09:00')
    today_str = config.today_iso()

    done = _completed_between(since)
    done_prev = len(_completed_between(prev_since, since))
    deleted = _deleted_since(since)
    open_tasks = _open_tasks()
    overdue, due_today = _overdue_and_today(open_tasks, today_str)

    start = (now - timedelta(days=7))
    msg = f'📊 週次レポート（{start.month}/{start.day}〜{now.month}/{now.day}）\n'

    diff = len(done) - done_prev
    diff_label = f'＋{diff}' if diff > 0 else (f'{diff}' if diff < 0 else '±0')
    msg += f'\n✅ 完了：{len(done)}件（1日あたり{len(done) / 7:.1f}件）'
    msg += f'\n　前の週：{done_prev}件（{diff_label}）'
    if deleted:
        msg += f'\n🗑 削除：{deleted}件（完了には数えていません）'
    msg += f'\n📌 未完了：{len(open_tasks)}件'
    if overdue:
        msg += f'\n🔴 期限切れ：{len(overdue)}件'

    if done:
        msg += '\n\n✅ 完了したタスク\n' + '\n'.join(f'・{_short(t["title"])}' for t in done[:10])
        if len(done) > 10:
            msg += f'\n…ほか{len(done) - 10}件'

    numbered = []
    if overdue:
        lines, _ = _numbered_block(overdue[:5], 1, user_id, numbered)
        msg += '\n\n🔴 期限切れのまま残っているもの\n' + '\n'.join(lines)
        if len(overdue) > 5:
            msg += f'\n…ほか{len(overdue) - 5}件'
        msg += '\n\n→ 番号でリスケできます（例：1明日／2を7/30）'
        if user_id:
            set_state(f'LAST_LIST_{user_id}', {'items': numbered, 'createdAt': config.now_ms()})

    logs = get_supabase('daily_logs', f'created_at=gte.{since}&select=log_date,content&order=log_date.desc')
    msg += f'\n\n📓 振り返りログ：{len(logs)}件'
    if logs:
        for l in logs[:7]:
            d = config.parse_date(l.get('log_date'))
            label = f'{d.month}/{d.day}' if d else str(l.get('log_date'))
            msg += f'\n・{label}：{_short(l.get("content"), 40)}'

    msg += '\n\n今週もお疲れ様でした！'
    return msg
