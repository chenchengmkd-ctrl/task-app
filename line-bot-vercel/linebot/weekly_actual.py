"""毎週土曜17時、「今週ここまでの実績」（月〜土）をLINEで送る。
週次CF予想（weekly_cf.py）が「来週の予定」を扱うのに対し、こちらは実際にあった数字を扱う。
5分おきのポーリング（/api/cron_poll → check_weekly_actual）から呼ばれる。
"""
from datetime import timedelta

from . import config
from . import finance as finance_mod
from .supabase_client import get_state, set_state

WEEKLY_ACTUAL_AT = (17, 0)   # 毎週土曜17時
SATURDAY = 5                 # datetime.weekday(): 月=0 … 土=5


def _due_now(now, hm, window_min=60):
    """指定時刻を過ぎてから window_min 分以内かどうか。ポーリングの遅れを吸収する。"""
    target = now.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
    elapsed = (now - target).total_seconds() / 60
    return 0 <= elapsed < window_min


def _week_dates(saturday_iso):
    """指定した土曜日を含む週の月〜土（6日分）の日付を返す。"""
    sat = config.parse_date(saturday_iso)
    monday = sat - timedelta(days=sat.weekday())
    return [config.iso_of_date(monday + timedelta(days=i)) for i in range(6)]


def build_weekly_actual_report(saturday_iso=None):
    """今週（月〜指定日。通常は土曜）の売上・客数・鰻/ご飯の使用量・人件費・支出をまとめる。

    客数・鰻/ご飯の使用量は Square 明細（birdmen:sales:YYYY-MM-DD、square.py が書き込む）から、
    売上・費用・人件費は日次入力（birdmen:report:YYYY-MM-DD）から読む。
    """
    saturday_iso = saturday_iso or config.today_iso()
    dates = _week_dates(saturday_iso)

    revenue = expense = 0
    week_by_cat = {k: 0 for k in finance_mod.CATEGORY_LABEL}
    customers = 0
    tails = 0.0
    rice_kg = 0.0

    for d in dates:
        rev, exp, _, by_cat = finance_mod.daily_totals(d)
        revenue += rev
        expense += exp
        for k, v in by_cat.items():
            week_by_cat[k] += v

        detail = finance_mod.kv_get(f'sales:{d}')
        if detail:
            customers += int(detail.get('customers') or 0)
            usage = detail.get('usage') or {}
            tails += float(usage.get('tails') or 0)
            rice_kg += float(usage.get('riceKg') or 0)

    label = f'{config.jp(dates[0])}〜{config.jp(dates[-1])}'
    lines = [f'📊 今週の実績（{label}）', '']
    lines.append(f'売上　{revenue:,}円')
    lines.append(f'客数　{customers}組')

    if tails or rice_kg:
        lines += ['', f'🐟 鰻　約{tails:.1f}尾', f'ご飯　約{rice_kg:.2f}kg']

    labor = week_by_cat.get('labor', 0)
    lines += ['', f'人件費　{labor:,}円', f'支出合計　{expense:,}円（人件費含む）']
    breakdown = [f'{finance_mod.CATEGORY_LABEL[k]} {v:,}' for k, v in week_by_cat.items() if v and k != 'labor']
    if breakdown:
        lines.append('　（' + '／'.join(breakdown) + '）')

    lines += ['', f'週次損益　{"+" if revenue - expense >= 0 else "-"}{abs(revenue - expense):,}円']
    lines += ['', finance_mod.APP_URL]
    return '\n'.join(lines)


def check_weekly_actual(push_text_fn, get_users_fn):
    """土曜17時になったら1回だけ送る（ポーリングで毎回呼ばれるので送信済みかを状態で見る）。"""
    now = config.now_jst()
    if now.weekday() != SATURDAY or not _due_now(now, WEEKLY_ACTUAL_AT):
        return
    date_iso = config.iso_of_date(now)
    key = f'WEEKLY_ACTUAL_SENT_{date_iso}'
    if get_state(key):
        return
    set_state(key, True)
    body = build_weekly_actual_report(date_iso)
    for uid in get_users_fn():
        push_text_fn(uid, body)
