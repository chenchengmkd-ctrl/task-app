"""週次キャッシュフロー予想。翌週7日分の入金・支出を見積もってLINEに送る。

「来週いくら入っていくら出るのか」「1日いくら売れば足りるのか」を、週が始まる前に把握するためのもの。
日曜の夜に翌週（月〜日）分を自動で送る。

数字の出どころは3種類：
1. Squareの実績から自動（現金売上の日次平均、金曜振込の見込み）
2. 財務アプリの入力履歴から自動（食材費・備品・その他の週平均、家賃・光熱費の月次パターン）
3. 設定（birdmen:cf-plan）… 履歴からは分からないものだけ。バイト収入と鰻仕入の週あたり回数

金額はすべて税込（＝実際に動くお金）。損益ではなく資金繰りを見るためのものなので税抜には直さない。
"""
from datetime import datetime, timedelta

from . import config
from . import finance as finance_mod
from . import square as square_mod

PLAN_KEY = 'cf-plan'

# 履歴から週平均を出すときに遡る日数。短すぎると1回のまとめ買いに引きずられ、
# 長すぎると最近の傾向を反映しなくなるので4週間にしてある
LOOKBACK_DAYS = 28

# 月に1回まとめて出る固定費。週平均に混ぜず、その日が週内に来たときだけ計上する
MONTHLY_LABELS = ('家賃', '光熱費')

# 鰻仕入は金額が大きく発生も不定期なので、食材費の週平均からは切り離して別枠で見積もる
UNAGI_LABEL = '鰻'

DEFAULT_PLAN = {
    'partTimeCount': 0,      # バイト収入の週あたり回数
    'partTimeAmount': 0,     # 1回あたりの金額
    'unagiPerWeek': 1,       # 鰻仕入の週あたり回数
    'unagiAmount': 0,        # 1回あたりの金額（0なら履歴の最頻値を使う）
}


def get_plan():
    stored = finance_mod.kv_get(PLAN_KEY) or {}
    return {**DEFAULT_PLAN, **stored}


def save_plan(plan):
    finance_mod.kv_set(PLAN_KEY, plan)


def _date(iso):
    return datetime.strptime(iso, '%Y-%m-%d').date()


def _iso(d):
    return d.strftime('%Y-%m-%d')


def _week_window(today):
    """翌週（月〜日）の期間。日曜に実行すれば翌日から、それ以外の曜日でも次の月曜から7日間。"""
    days_until_monday = (0 - today.weekday()) % 7 or 7
    start = today + timedelta(days=days_until_monday)
    return start, start + timedelta(days=6)


def _recent_cashflow(today):
    """直近LOOKBACK_DAYS日ぶんの現金／現金以外の記録。"""
    months = set()
    for i in range(0, LOOKBACK_DAYS + 31, 28):
        months.add(_iso(today - timedelta(days=i))[:7])
    since = _iso(today - timedelta(days=LOOKBACK_DAYS))
    out = []
    for m in months:
        for _, v in finance_mod.kv_list(f'cashflow:{m}'):
            if v and since <= v.get('date', '') <= _iso(today):
                out.append(v)
    return out


def _daily_averages(today):
    """営業日1日あたりの現金・現金以外の平均と、週あたりの営業日数。"""
    records = [r for r in _recent_cashflow(today) if (r.get('total') or 0) > 0]
    if not records:
        return {'cash': 0, 'noncash': 0, 'openDays': 0, 'sampleDays': 0}
    days = len(records)
    span_weeks = max(1.0, LOOKBACK_DAYS / 7)
    return {
        'cash': round(sum(int(r.get('cash') or 0) for r in records) / days),
        'noncash': round(sum(int(r.get('noncash') or 0) for r in records) / days),
        # 週に何日開けているか。7日を超えることはないので上限をかける
        'openDays': min(7, round(days / span_weeks)),
        'sampleDays': days,
    }


def _recent_reports(today):
    """直近LOOKBACK_DAYS日ぶんの日次データ。"""
    months = {_iso(today)[:7], _iso(today - timedelta(days=LOOKBACK_DAYS))[:7]}
    since = _iso(today - timedelta(days=LOOKBACK_DAYS))
    out = []
    for m in sorted(months):
        for r in finance_mod._month_reports(m):
            if since <= r.get('date', '') <= _iso(today):
                out.append(r)
    return out


def _weekly_expense_averages(today):
    """食材費・備品・その他の週あたり平均（税込）。

    鰻仕入と月1回の固定費（家賃・光熱費）は別枠で見積もるので、ここからは除く。
    """
    reports = _recent_reports(today)
    weeks = max(1.0, LOOKBACK_DAYS / 7)
    totals = {'ingredient': 0, 'supplies': 0, 'other': 0}
    for r in reports:
        for bucket in ('corp', 'pers', 'cash'):
            for w in ((r.get(bucket) or {}).get('withdraws') or []):
                amount = int(w.get('amount') or 0)
                if not amount:
                    continue
                cat = w.get('category') or 'other'
                if cat not in totals:
                    continue
                label = w.get('label') or ''
                if UNAGI_LABEL in label or any(m in label for m in MONTHLY_LABELS):
                    continue
                totals[cat] += amount
    return {k: round(v / weeks) for k, v in totals.items()}


def _unagi_amount(today, plan):
    """鰻仕入1回あたりの金額。設定にあればそれ、無ければ履歴の直近の金額。"""
    if plan.get('unagiAmount'):
        return int(plan['unagiAmount'])
    amounts = []
    for r in _recent_reports(today):
        for bucket in ('corp', 'pers', 'cash'):
            for w in ((r.get(bucket) or {}).get('withdraws') or []):
                if UNAGI_LABEL in (w.get('label') or '') and int(w.get('amount') or 0) > 0:
                    amounts.append(int(w['amount']))
    return amounts[-1] if amounts else 0


def _monthly_fixed_in_window(today, start, end):
    """家賃・光熱費など月1回の支出のうち、対象週に支払日が来るもの。

    履歴から「毎月だいたい何日にいくら」を拾う。資金繰り画面（payments）に登録があれば
    そちらを優先する。
    """
    found = {}
    # 履歴から拾う（直近の発生日・金額を採用）
    months = sorted({_iso(today)[:7], _iso(today - timedelta(days=62))[:7], _iso(today - timedelta(days=31))[:7]})
    for m in months:
        for r in finance_mod._month_reports(m):
            date_iso = r.get('date') or ''
            for bucket in ('corp', 'pers', 'cash'):
                for w in ((r.get(bucket) or {}).get('withdraws') or []):
                    label = w.get('label') or ''
                    amount = int(w.get('amount') or 0)
                    if amount <= 0:
                        continue
                    for key in MONTHLY_LABELS:
                        if key in label:
                            found[key] = {'day': int(date_iso[8:10]), 'amount': amount}

    # 資金繰り画面に登録があれば上書き（人が入れた予定のほうが確か）
    for p in (finance_mod.kv_get('payments') or []):
        if p.get('active') is False or p.get('direction') != 'out':
            continue
        day = p.get('dayOfMonth')
        amount = int(p.get('amount') or 0)
        if not day or amount <= 0:
            continue
        found[p.get('name') or '固定費'] = {'day': int(day), 'amount': amount}

    out = []
    for name, info in found.items():
        for offset in range((end - start).days + 1):
            d = start + timedelta(days=offset)
            if d.day == info['day']:
                out.append({'name': name, 'date': _iso(d), 'amount': info['amount']})
    return sorted(out, key=lambda x: x['date'])


def _friday_in_window(start, end):
    for offset in range((end - start).days + 1):
        d = start + timedelta(days=offset)
        if d.weekday() == 4:
            return d
    return None


def _friday_deposit(today, friday):
    """その金曜に振り込まれる見込み額。精算期間は木曜〜翌水曜（Squareの締めに合わせる）。

    期間の一部がまだ未来のときは、確定している日の平均で残り日数を埋める。
    """
    if friday is None:
        return 0
    window_end = friday - timedelta(days=2)
    window_start = window_end - timedelta(days=6)
    ws, we = _iso(window_start), _iso(window_end)

    records = {r['date']: r for r in _recent_cashflow(today) if r.get('date')}
    actual = sum(int(r.get('noncash') or 0) for d, r in records.items() if ws <= d <= we)
    known_days = sum(1 for d in records if ws <= d <= we)

    total_days = (window_end - window_start).days + 1
    missing = total_days - known_days
    if missing <= 0:
        return actual
    avg = _daily_averages(today)['noncash']
    return actual + avg * missing


def build_weekly_cf(date_iso=None):
    """翌週の資金繰り予想。ユーザーが示した書式（収入予想／支出予想／週次収支）で返す。"""
    date_iso = date_iso or config.today_iso()
    today = _date(date_iso)
    start, end = _week_window(today)
    plan = get_plan()

    avg = _daily_averages(today)
    open_days = avg['openDays'] or 7
    friday = _friday_in_window(start, end)

    # --- 収入 ---
    deposit = _friday_deposit(today, friday)
    cash_sales = avg['cash'] * open_days
    part_time = int(plan['partTimeCount']) * int(plan['partTimeAmount'])
    income_total = deposit + cash_sales + part_time

    # --- 支出 ---
    unagi_unit = _unagi_amount(today, plan)
    unagi_count = int(plan['unagiPerWeek'])
    unagi = unagi_unit * unagi_count
    weekly = _weekly_expense_averages(today)
    fixed = _monthly_fixed_in_window(today, start, end)
    fixed_total = sum(f['amount'] for f in fixed)
    expense_total = unagi + fixed_total + weekly['ingredient'] + weekly['supplies'] + weekly['other']

    net = income_total - expense_total

    lines = [
        f'📅 週次CF予想（{config.jp(_iso(start))}〜{config.jp(_iso(end))}）',
        '',
        '【収入予想】',
    ]
    if friday is not None:
        lines.append(f'・Square入金予想（{config.jp(_iso(friday))}金）：{deposit:,}円')
    if cash_sales:
        lines.append(f'・現金売上（{avg["cash"]:,}×{open_days}日）：{cash_sales:,}円')
    if part_time:
        lines.append(f'・バイト収入（{plan["partTimeCount"]}回×{int(plan["partTimeAmount"]):,}円）：{part_time:,}円')
    lines.append(f'収入合計：{income_total:,}円')

    lines += ['', '【支出予想】']
    if unagi:
        lines.append(f'・鰻仕入（{unagi_count}回×{unagi_unit:,}円）：{unagi:,}円')
    for f in fixed:
        lines.append(f'・{f["name"]}（{config.jp(f["date"])}）：{f["amount"]:,}円')
    if weekly['ingredient']:
        lines.append(f'・食材費：{weekly["ingredient"]:,}円')
    if weekly['supplies']:
        lines.append(f'・備品・消耗品：{weekly["supplies"]:,}円')
    if weekly['other']:
        lines.append(f'・その他：{weekly["other"]:,}円')
    lines.append(f'支出合計：{expense_total:,}円')

    lines += ['', '【週次収支】', f'収支：{"+" if net >= 0 else ""}{net:,}円', '']

    # 「1日いくら売れば足りるか」。現金売上以外の入金は動かせないので、そこを差し引いた
    # 残りを営業日数で割る＝毎日これだけ現金が入れば週の支払いが回る、という下限
    breakeven = max(0, expense_total - deposit - part_time)
    per_day = -(-breakeven // open_days) if open_days else 0  # 切り上げ
    if net >= 0:
        lines.append(f'1日の現金売上が{per_day:,}円以上あれば週次収支はプラスで支払いもできる見込みです。')
    else:
        lines.append(f'このままだと{abs(net):,}円不足します。1日の現金売上が{per_day:,}円必要です。')

    if avg['sampleDays'] == 0:
        lines.append('※ Squareの実績がまだ無いため、現金売上は0で計算しています')
    if not part_time and not plan['partTimeCount']:
        lines.append('※ バイト収入は未設定です（「CF設定 バイト 5回 5000円」で登録できます）')

    return '\n'.join(lines)


def send_weekly_cf(push, get_users_fn):
    """cronから呼ぶ。日曜の夜に翌週分を全ユーザーへ送る。"""
    body = build_weekly_cf()
    for uid in get_users_fn():
        push(uid, body)


def handle_plan_command(text):
    """「CF設定 バイト 5回 5000円」「CF設定 鰻 2回」などの設定変更。対象外ならNone。"""
    import re
    m = re.match(r'^(?:CF設定|ＣＦ設定|週次CF設定)\s+(バイト|鰻)\s*(\d+)\s*回?(?:\s*([\d,]+)\s*円?)?$', text)
    if not m:
        return None
    plan = get_plan()
    count = int(m.group(2))
    amount = int(m.group(3).replace(',', '')) if m.group(3) else None
    if m.group(1) == 'バイト':
        plan['partTimeCount'] = count
        if amount is not None:
            plan['partTimeAmount'] = amount
        save_plan(plan)
        return f'✅ バイト収入を 週{count}回 × {int(plan["partTimeAmount"]):,}円 に設定しました'
    plan['unagiPerWeek'] = count
    if amount is not None:
        plan['unagiAmount'] = amount
    save_plan(plan)
    unit = plan['unagiAmount'] or '履歴から自動'
    return f'✅ 鰻仕入を 週{count}回（1回 {unit if isinstance(unit, str) else format(unit, ",")}円）に設定しました'
