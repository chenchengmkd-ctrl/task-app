"""週次キャッシュフロー予想。翌週7日分の入金・支出を見積もってLINEに送る。

「来週いくら入っていくら出るのか」「1日いくら売れば足りるのか」を、週が始まる前に把握するためのもの。
日曜の夜に翌週（月〜日）分を自動で送る。

数字はアプリの「資金繰りカレンダー」とまったく同じ計算にしてある（birdmen:cf-plan を共有）：
- 現金売上・Square入金 … Squareの実績から曜日別平均で見込みを立てる。カレンダーで手修正した日はその値
- 個人収入・食品・備品・その他 … カレンダーに入力された明細をそのまま足す
- まだ何も入力されていないカテゴリだけ、履歴の週平均で埋める（入力前でも目安が出るように）

金額はすべて税込（＝実際に動くお金）。損益ではなく資金繰りを見るためのものなので税抜には直さない。
"""
from datetime import datetime, timedelta

from . import config
from . import finance as finance_mod

PLAN_KEY = 'cf-plan'

# 履歴から週平均を出すときに遡る日数。短すぎると1回のまとめ買いに引きずられ、
# 長すぎると最近の傾向を反映しなくなるので4週間にしてある
LOOKBACK_DAYS = 28

ROW_LABEL = {
    'cashSales': '現金売上',
    'deposit': 'Square入金',
    'personal': '個人収入',
    'ingredient': '食品',
    'supplies': '備品',
    'other': 'その他',
}
INCOME_ROWS = ('cashSales', 'deposit', 'personal')
EXPENSE_ROWS = ('ingredient', 'supplies', 'other')

# 履歴から週平均を出すときのカテゴリ対応（財務アプリのExpenseCategory → CFの行）
HISTORY_CATEGORY = {'ingredient': 'ingredient', 'supplies': 'supplies', 'other': 'other'}


def _plan():
    raw = finance_mod.kv_get(PLAN_KEY) or {}
    return {'entries': raw.get('entries') or {}, 'overrides': raw.get('overrides') or {}}


def _date(iso):
    return datetime.strptime(iso, '%Y-%m-%d').date()


def _iso(d):
    return d.strftime('%Y-%m-%d')


def _week_dates(today):
    """翌週（月〜日）の7日分。日曜に実行すれば翌日から、それ以外でも次の月曜から。"""
    until_monday = (0 - today.weekday()) % 7 or 7
    start = today + timedelta(days=until_monday)
    return [start + timedelta(days=i) for i in range(7)]


def _cashflow_records(today, back_days):
    """指定日数ぶん遡って現金／現金以外の記録を日付キーで返す。"""
    months = set()
    for i in range(0, back_days + 31, 28):
        months.add(_iso(today - timedelta(days=i))[:7])
    months.add(_iso(today + timedelta(days=14))[:7])
    since = _iso(today - timedelta(days=back_days))
    out = {}
    for m in months:
        for _, v in finance_mod.kv_list(f'cashflow:{m}'):
            if v and v.get('date', '') >= since:
                out[v['date']] = v
    return out


def _weekday_averages(records):
    """曜日ごとの現金・現金以外の1日平均。実績が無い曜日は全体平均で埋める。

    全期間の単純平均だと土日と平日の差が消えてしまうため曜日別に出す
    （アプリ側 cashflowCalc.ts の weekdayAverages と同じ考え方）。
    """
    open_days = [r for r in records.values() if (r.get('total') or 0) > 0]
    acc = [{'cash': 0, 'noncash': 0, 'days': 0} for _ in range(7)]
    for r in open_days:
        a = acc[_date(r['date']).weekday()]
        a['cash'] += int(r.get('cash') or 0)
        a['noncash'] += int(r.get('noncash') or 0)
        a['days'] += 1
    if open_days:
        overall = {
            'cash': round(sum(int(r.get('cash') or 0) for r in open_days) / len(open_days)),
            'noncash': round(sum(int(r.get('noncash') or 0) for r in open_days) / len(open_days)),
        }
    else:
        overall = {'cash': 0, 'noncash': 0}
    return [
        {'cash': round(a['cash'] / a['days']), 'noncash': round(a['noncash'] / a['days'])}
        if a['days'] else dict(overall)
        for a in acc
    ]


def _deposit_for(friday, records, avgs):
    """その金曜に振り込まれる現金以外の売上。精算期間は木曜〜翌水曜（Squareの締め）。"""
    total = 0
    for i in range(8, 1, -1):
        d = friday - timedelta(days=i)
        rec = records.get(_iso(d))
        total += int(rec.get('noncash') or 0) if rec else avgs[d.weekday()]['noncash']
    return total


def _entry_total(plan, date_iso, kind):
    items = (plan['entries'].get(date_iso) or {}).get(kind) or []
    return sum(int(i.get('amount') or 0) for i in items)


def _entry_names(plan, dates, kind):
    """その週に入力されている明細名（重複を除いて順序を保つ）。"""
    seen, names = set(), []
    for d in dates:
        for i in ((plan['entries'].get(_iso(d)) or {}).get(kind) or []):
            name = (i.get('name') or '').strip()
            if name and name not in seen:
                seen.add(name)
                names.append(name)
    return names


def _history_weekly_averages(today):
    """食品・備品・その他の週あたり平均（税込）。カレンダーが空のカテゴリを埋めるのに使う。"""
    months = {_iso(today)[:7], _iso(today - timedelta(days=LOOKBACK_DAYS))[:7]}
    since = _iso(today - timedelta(days=LOOKBACK_DAYS))
    totals = {k: 0 for k in EXPENSE_ROWS}
    for m in sorted(months):
        for r in finance_mod._month_reports(m):
            if not (since <= r.get('date', '') <= _iso(today)):
                continue
            for bucket in ('corp', 'pers', 'cash'):
                for w in ((r.get(bucket) or {}).get('withdraws') or []):
                    row = HISTORY_CATEGORY.get(w.get('category') or 'other')
                    if row:
                        totals[row] += int(w.get('amount') or 0)
    weeks = max(1.0, LOOKBACK_DAYS / 7)
    return {k: round(v / weeks) for k, v in totals.items()}


def build_weekly_cf(date_iso=None):
    """翌週の資金繰り予想。アプリの資金繰りカレンダーと同じ数字になる。"""
    date_iso = date_iso or config.today_iso()
    today = _date(date_iso)
    dates = _week_dates(today)
    plan = _plan()

    records = _cashflow_records(today, LOOKBACK_DAYS)
    avgs = _weekday_averages(records)

    # --- 収入 ---
    cash_sales = 0
    for d in dates:
        override = (plan['overrides'].get(_iso(d)) or {}).get('cashSales')
        if override is not None:
            cash_sales += int(override)
        else:
            rec = records.get(_iso(d))
            cash_sales += int(rec.get('cash') or 0) if rec else avgs[d.weekday()]['cash']

    deposit = 0
    friday = None
    for d in dates:
        override = (plan['overrides'].get(_iso(d)) or {}).get('deposit')
        if override is not None:
            deposit += int(override)
            friday = friday or d
        elif d.weekday() == 4:
            deposit += _deposit_for(d, records, avgs)
            friday = d

    personal = sum(_entry_total(plan, _iso(d), 'personal') for d in dates)
    income_total = cash_sales + deposit + personal

    # --- 支出 ---
    history = _history_weekly_averages(today)
    expenses = {}
    estimated = set()
    for kind in EXPENSE_ROWS:
        entered = sum(_entry_total(plan, _iso(d), kind) for d in dates)
        if entered > 0:
            expenses[kind] = entered
        else:
            # まだ何も入力されていないカテゴリは履歴の週平均で埋める（入力前でも目安が出るように）
            expenses[kind] = history[kind]
            if history[kind]:
                estimated.add(kind)
    expense_total = sum(expenses.values())
    net = income_total - expense_total

    open_days = sum(
        1 for d in dates
        if (plan['overrides'].get(_iso(d)) or {}).get('cashSales') is not None
        or (records.get(_iso(d)) or {}).get('cash')
        or avgs[d.weekday()]['cash']
    ) or 7

    lines = [
        f'📅 週次CF予想（{config.jp(_iso(dates[0]))}〜{config.jp(_iso(dates[-1]))}）',
        '',
        '【収入予想】',
    ]
    if cash_sales:
        lines.append(f'・現金売上（{open_days}日）：{cash_sales:,}円')
    if deposit:
        label = f'（{config.jp(_iso(friday))}金）' if friday else ''
        lines.append(f'・Square入金予想{label}：{deposit:,}円')
    if personal:
        names = _entry_names(plan, dates, 'personal')
        detail = f'（{"・".join(names[:3])}）' if names else ''
        lines.append(f'・個人収入{detail}：{personal:,}円')
    lines.append(f'収入合計：{income_total:,}円')

    lines += ['', '【支出予想】']
    for kind in EXPENSE_ROWS:
        amount = expenses[kind]
        if not amount:
            continue
        if kind in estimated:
            lines.append(f'・{ROW_LABEL[kind]}：{amount:,}円（履歴平均）')
        else:
            names = _entry_names(plan, dates, kind)
            detail = f'（{"・".join(names[:3])}）' if names else ''
            lines.append(f'・{ROW_LABEL[kind]}{detail}：{amount:,}円')
    lines.append(f'支出合計：{expense_total:,}円')

    lines += ['', '【週次収支】', f'収支：{"+" if net >= 0 else ""}{net:,}円', '']

    # 現金売上以外の入金は日々の営業では動かせないので、そこを差し引いた残りを営業日数で割る
    breakeven = max(0, expense_total - deposit - personal)
    per_day = -(-breakeven // open_days) if open_days else 0
    if net >= 0:
        lines.append(f'1日の現金売上が{per_day:,}円以上あれば週次収支はプラスで支払いもできる見込みです。')
    else:
        lines.append(f'このままだと{abs(net):,}円不足します。1日の現金売上が{per_day:,}円必要です。')

    if estimated:
        labels = '・'.join(ROW_LABEL[k] for k in EXPENSE_ROWS if k in estimated)
        lines.append(f'※ {labels} は未入力のため履歴の週平均です。アプリの資金繰りカレンダーで予定を入れると正確になります')

    return '\n'.join(lines)


def send_weekly_cf(push, get_users_fn):
    """cronから呼ぶ。日曜の夜に翌週分を全ユーザーへ送る。"""
    body = build_weekly_cf()
    for uid in get_users_fn():
        push(uid, body)
