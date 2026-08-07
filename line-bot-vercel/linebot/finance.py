"""財務管理アプリ（oneburg-finance）の売上・損益をLINEで確認／入力する。

データは同じSupabaseプロジェクトの birdmen_kv テーブル（key/value）に入っている。
Webアプリ側（oneburg-finance/src/utils/storage.ts）が書いた JSON をそのまま読み書きする。

金額の扱いはWebアプリ側と揃えてある：
- LineItem.amount は常に税込。LINEでの入力は税抜で受け取り、品目マスタの税率で税込に直して保存する
- 売上（cash.sales）は税込のまま扱う
- 売上合計 = 現金売上 + 法人入金 + 個人入金（現金→法人の内部振替 toBank は二重計上になるので含めない）
- 費用 = 全バケットの引出明細をカテゴリ別に集計
- 損益 = 売上 − 費用

書き込みは「1日分のJSONを丸ごと上書き」する方式なので、Web画面で同じ日を同時に編集していると
あとから書いたほうで上書きされる。単独利用を前提とした割り切り（取り消しは1回分だけ保持）。
"""
import calendar
import re
from urllib.parse import quote

from . import config
from .supabase_client import get_supabase, get_state, set_state, delete_state

TABLE = 'birdmen_kv'
PREFIX = 'birdmen:'

CATEGORY_LABEL = {
    'ingredient': '食材',
    'supplies': '備品',
    'labor': '人件費',
    'rent': '家賃',
    'utility': '光熱費',
    'other': 'その他',
}

# Webアプリの DEFAULT_TAX_RATE と同じ（品目マスタに税率が無い場合のフォールバック）
DEFAULT_TAX_RATE = {
    'ingredient': 8, 'supplies': 10, 'labor': 0,
    'rent': 10, 'utility': 10, 'other': 10,
}

# Webアプリの storage.defaultStaff() と同じ（birdmen:staff が未保存のときに使う）
DEFAULT_STAFF = [
    {'id': 'tomaru', 'name': '都丸里帆', 'hourlyWage': 1500, 'transport': 960},
    {'id': 'kudo', 'name': '工藤香菜', 'hourlyWage': 1500, 'transport': 0},
    {'id': 'uehara', 'name': '上原敦子', 'hourlyWage': 1500, 'transport': 356},
]

QUICK_LABOR_ID = 'quick-labor'
UNDO_KEY = 'FINANCE_UNDO'

APP_URL = 'https://oneburg-finance.vercel.app'


def entry_url(date_iso):
    """その日の「日次入力」を直接開くURL（Webアプリ側がクエリを読んで該当日を表示する）。"""
    return f'{APP_URL}/?page=daily&date={date_iso}'


def _report(date_iso):
    """指定日の残高報告を返す。無ければNone。"""
    key = f'{PREFIX}report:{date_iso}'
    rows = get_supabase(TABLE, f'key=eq.{quote(key)}&select=value')
    return rows[0].get('value') if rows else None


def _month_reports(month):
    """指定月（YYYY-MM）の残高報告を日付順で返す。"""
    like = f'{PREFIX}report:{month}'
    rows = get_supabase(TABLE, f'key=like.{quote(like)}*&select=key,value')
    reports = [r.get('value') for r in rows if r.get('value')]
    return sorted(reports, key=lambda r: r.get('date', ''))


def _sum_items(items):
    return sum(int(i.get('amount') or 0) for i in (items or []))


def _revenue(report):
    cash = report.get('cash') or {}
    corp = report.get('corp') or {}
    pers = report.get('pers') or {}
    return (
        int(cash.get('sales') or 0)
        + _sum_items(corp.get('deposits'))
        + _sum_items(pers.get('deposits'))
    )


def _expense_by_category(report):
    totals = {k: 0 for k in CATEGORY_LABEL}
    for bucket in ('corp', 'pers', 'cash'):
        day = report.get(bucket) or {}
        for item in (day.get('withdraws') or []):
            cat = item.get('category') or 'other'
            totals[cat] = totals.get(cat, 0) + int(item.get('amount') or 0)
    return totals


def _totals(report):
    revenue = _revenue(report)
    by_cat = _expense_by_category(report)
    expense = sum(by_cat.values())
    return revenue, expense, revenue - expense, by_cat


def _yen(n):
    return f'{n:,}円'


def _signed(n):
    return f'{"+" if n >= 0 else "-"}{abs(n):,}円'


def build_daily_finance_report(date_iso=None):
    """当日の売上・費用・損益と、当月の累計をまとめたテキストを返す。"""
    date_iso = date_iso or config.today_iso()
    month = date_iso[:7]

    report = _report(date_iso)
    lines = [f'📊 {config.jp(date_iso)} の収支']

    if not report:
        lines.append('')
        lines.append('この日の入力はまだありません。')
    else:
        revenue, expense, profit, by_cat = _totals(report)
        lines.append('')
        lines.append(f'売上　{_yen(revenue)}')
        lines.append(f'費用　{_yen(expense)}')
        breakdown = [f'{CATEGORY_LABEL[k]} {v:,}' for k, v in by_cat.items() if v]
        if breakdown:
            lines.append('　（' + '／'.join(breakdown) + '）')
        lines.append(f'当日損益　{_signed(profit)}')

    reports = _month_reports(month)
    if reports:
        m_rev = m_exp = 0
        for r in reports:
            rev, exp, _, _ = _totals(r)
            m_rev += rev
            m_exp += exp
        lines.append('')
        lines.append(f'▼ {int(month[5:7])}月の累計（{len(reports)}日分）')
        lines.append(f'売上　{_yen(m_rev)}')
        lines.append(f'費用　{_yen(m_exp)}')
        lines.append(f'損益　{_signed(m_rev - m_exp)}')
        lines.extend(_budget_lines(month, m_rev, m_exp, date_iso))

    lines.append('')
    lines.append(entry_url(date_iso))
    return '\n'.join(lines)


def _budget_for(month):
    """その月に適用される予算（Webアプリの storage.budgetFor と同じ考え方）。未設定ならNone。"""
    raw = _kv_get('budget') or {}
    base = raw.get('default') or {}
    over = (raw.get('months') or {}).get(month) or {}
    revenue = over.get('revenue', base.get('revenue', 0)) or 0
    expenses = {**(base.get('expenses') or {}), **(over.get('expenses') or {})}
    total_expense = sum(int(expenses.get(c) or 0) for c in CATEGORY_LABEL)
    if not revenue and not total_expense:
        return None
    return {'revenue': int(revenue), 'expenses': expenses, 'expenseTotal': total_expense}


def _budget_lines(month, revenue_actual, expense_actual, today_iso=None):
    """予実の進捗を数行のテキストで返す。予算未設定なら空リスト。"""
    b = _budget_for(month)
    if not b:
        return []
    today_iso = today_iso or config.today_iso()
    y, m = int(month[:4]), int(month[5:7])
    total_days = calendar.monthrange(y, m)[1]
    if month < today_iso[:7]:
        elapsed = total_days
    elif month > today_iso[:7]:
        elapsed = 0
    else:
        elapsed = min(total_days, int(today_iso[8:10]))
    pace = elapsed / total_days if total_days else 0

    def pct(actual, plan):
        return round(actual / plan * 100) if plan else None

    lines = ['', f'▼ 予算（{elapsed}/{total_days}日経過 {round(pace * 100)}%）']
    r_pct = pct(revenue_actual, b['revenue'])
    if r_pct is not None:
        mark = '⚠️' if r_pct < round(pace * 100) else ''
        lines.append(f'売上　{r_pct}%（目標 {b["revenue"]:,}円）{mark}')
    e_pct = pct(expense_actual, b['expenseTotal'])
    if e_pct is not None:
        mark = '⚠️' if e_pct > round(pace * 100) else ''
        lines.append(f'費用　{e_pct}%（予算 {b["expenseTotal"]:,}円）{mark}')
        remain = b['expenseTotal'] - expense_actual
        lines.append(f'　費用の残り {remain:,}円')
    target_profit = b['revenue'] - b['expenseTotal']
    if target_profit:
        lines.append(f'目標損益　{_signed(target_profit)}')
    return lines


def build_month_finance_report(month=None):
    """当月の累計と、費用のカテゴリ別内訳を返す。"""
    month = month or config.today_iso()[:7]
    reports = _month_reports(month)
    if not reports:
        return f'📊 {int(month[5:7])}月の入力はまだありません。\n{APP_URL}'

    m_rev = m_exp = 0
    totals = {k: 0 for k in CATEGORY_LABEL}
    for r in reports:
        rev, exp, _, by_cat = _totals(r)
        m_rev += rev
        m_exp += exp
        for k, v in by_cat.items():
            totals[k] = totals.get(k, 0) + v

    lines = [f'📊 {int(month[5:7])}月の収支（{len(reports)}日分）', '']
    lines.append(f'売上　{_yen(m_rev)}')
    lines.append(f'費用　{_yen(m_exp)}')
    for k, label in CATEGORY_LABEL.items():
        if totals.get(k):
            lines.append(f'　{label}　{totals[k]:,}円')
    lines.append('')
    lines.append(f'損益　{_signed(m_rev - m_exp)}')
    lines.extend(_budget_lines(month, m_rev, m_exp))
    lines.append('')
    lines.append(APP_URL)
    return '\n'.join(lines)


def send_finance_report(push_text, get_users):
    """cronから呼ぶ。当日の収支を全ユーザーにpushする。"""
    body = build_daily_finance_report()
    for uid in get_users():
        push_text(uid, body)


# ============ ここから入力（書き込み）機能 ============

def _kv_get(key):
    rows = get_supabase(TABLE, f'key=eq.{quote(PREFIX + key)}&select=value')
    return rows[0].get('value') if rows else None


def _kv_set(key, value):
    """birdmen_kv に upsert する。Webアプリの storage.set と同じ形式。"""
    import json
    import requests
    url = f'{config.SUPABASE_URL}/rest/v1/{TABLE}?on_conflict=key'
    res = requests.post(
        url,
        headers={
            'apikey': config.SUPABASE_ANON_KEY,
            'Authorization': f'Bearer {config.SUPABASE_ANON_KEY}',
            'Content-Type': 'application/json',
            'Prefer': 'resolution=merge-duplicates,return=minimal',
        },
        data=json.dumps([{'key': PREFIX + key, 'value': value, 'updated_at': config.now_iso()}]),
        timeout=20,
    )
    if res.status_code >= 300:
        print('finance kv_set error', res.status_code, res.text)
        return False
    return True


def _empty_bucket():
    return {'prevOverride': None, 'deposits': [], 'withdraws': [], 'balanceOverride': None}


def _default_report(date_iso):
    """Webアプリの storage.defaultReport と同じ構造（空シフト枠は作らない）。"""
    return {
        'date': date_iso,
        'pers': _empty_bucket(),
        'corp': _empty_bucket(),
        'cash': {**_empty_bucket(), 'sales': 0, 'salesNote': '', 'toBank': 0},
        'pending': {'persExpected': 0, 'corpSquare': 0, 'cashReturn': 0},
        'shifts': [],
        'note': '',
    }


def _new_item_id():
    return f'li_{config.now_ms()}_{config.new_id()[-4:]}'


def _staff_list():
    return _kv_get('staff') or DEFAULT_STAFF


def _item_master():
    """品目マスタを {cat: [{name, taxRate}]} に正規化して返す。旧形式（文字列配列）にも対応。"""
    raw = _kv_get('item-labels') or {}
    items = {}
    for cat in DEFAULT_TAX_RATE:
        out = []
        for v in (raw.get('items', {}) or {}).get(cat, []) or []:
            if isinstance(v, str):
                out.append({'name': v, 'taxRate': DEFAULT_TAX_RATE[cat]})
            elif isinstance(v, dict) and v.get('name'):
                out.append({'name': v['name'], 'taxRate': v.get('taxRate', DEFAULT_TAX_RATE[cat])})
        items[cat] = out
    vendors = {cat: ((raw.get('vendors', {}) or {}).get(cat) or []) for cat in DEFAULT_TAX_RATE}
    return {'items': items, 'vendors': vendors}


def _tax_rate_for(cat, label):
    """品目マスタに登録があればその税率、無ければカテゴリ既定。"""
    for d in _item_master()['items'].get(cat, []):
        if d['name'] == label:
            return d.get('taxRate', DEFAULT_TAX_RATE[cat])
    return DEFAULT_TAX_RATE[cat]


def _to_gross(net, rate):
    """税抜→税込（1円未満四捨五入）。Webアプリの storage.toGross と同じ。"""
    return int(round(net * (1 + rate / 100)))


def _shift_pay(s):
    """出退勤から日給を計算。Webアプリの storage.shiftPay と同じ（1分単位・最後に四捨五入）。"""
    ci, co = s.get('clockIn') or '', s.get('clockOut') or ''
    if not ci or not co:
        return 0
    try:
        ih, im = [int(x) for x in ci.split(':')]
        oh, om = [int(x) for x in co.split(':')]
    except ValueError:
        return 0
    mins = (oh * 60 + om) - (ih * 60 + im)
    if mins <= 0:
        return 0
    return int(round(mins / 60 * int(s.get('hourlyWage') or 0) + int(s.get('transport') or 0)))


def _sync_labor(report):
    """シフト合計を corp.withdraws の固定ID quick-labor に反映（applyShiftsToReport 相当）。"""
    total = sum(_shift_pay(s) for s in (report.get('shifts') or []))
    corp = report.get('corp') or _empty_bucket()
    rest = [w for w in (corp.get('withdraws') or []) if w.get('id') != QUICK_LABOR_ID]
    if total > 0:
        rest.append({
            'id': QUICK_LABOR_ID, 'label': '人件費（シフト合計）',
            'amount': total, 'category': 'labor', 'taxRate': 0,
        })
    corp['withdraws'] = rest
    report['corp'] = corp
    return report


_ZEN = str.maketrans('０１２３４５６７８９：／　', '0123456789:/ ')


def _norm(text):
    return (text or '').translate(_ZEN).strip()


def _parse_amount(tok):
    """'1,080' '1080円' → 1080。数値でなければ None。"""
    t = re.sub(r'[,，円]', '', tok or '')
    if not re.fullmatch(r'-?\d+', t):
        return None
    return int(t)


def _parse_time(tok):
    """'10:30' '10時30分' '10時' → 'HH:MM'。合わなければ None。"""
    m = re.fullmatch(r'(\d{1,2}):(\d{1,2})', tok or '')
    if not m:
        m = re.fullmatch(r'(\d{1,2})時(?:(\d{1,2})分?)?', tok or '')
    if not m:
        return None
    h = int(m.group(1))
    mi = int(m.group(2) or 0)
    if h > 47 or mi > 59:
        return None
    return f'{h:02d}:{mi:02d}'


def _save_with_undo(user_id, date_iso, before, after):
    """書き込み前の状態を控えてから保存する（「取り消し」用。1回分だけ保持）。"""
    set_state(f'{UNDO_KEY}:{user_id}', {'date': date_iso, 'before': before})
    return _kv_set(f'report:{date_iso}', after)


def _day_summary(report):
    revenue, expense, profit, _ = _totals(report)
    return f'売上 {revenue:,} / 費用 {expense:,} / 損益 {_signed(profit)}'


def _resolve_date(text):
    """先頭の「8/5」「8月5日」を日付として切り出す。無ければ今日。戻り値は (date_iso, 残りのテキスト)。"""
    today = config.now_jst()
    m = re.match(r'^(\d{1,2})[/月](\d{1,2})日?[\s]+([\s\S]+)$', text)
    if not m:
        return config.today_iso(), text
    mo, da = int(m.group(1)), int(m.group(2))
    year = today.year
    # 12月に「1/5」と書いたら翌年、1月に「12/28」と書いたら前年とみなす
    if today.month == 12 and mo == 1:
        year += 1
    elif today.month == 1 and mo == 12:
        year -= 1
    try:
        return f'{year:04d}-{mo:02d}-{da:02d}', m.group(3).strip()
    except ValueError:
        return config.today_iso(), text


def _add_expense_item(user_id, date_iso, cat, rest):
    """「[仕入れ先] 品目 金額」を1件追加する。金額は税抜として受け取り税込で保存。

    金額が読み取れない場合はNoneを返す（「仕入れリストを作る」のような普通のタスクを
    財務コマンドとして横取りしないため。呼び出し元でタスク追加へ流れる）。
    """
    parts = rest.split()
    if len(parts) < 2:
        return None
    net = _parse_amount(parts[-1])
    if net is None:
        return None
    vendor, label = (None, parts[0]) if len(parts) == 2 else (' '.join(parts[:-2]), parts[-2])

    report = _report(date_iso) or _default_report(date_iso)
    before = _report(date_iso)
    rate = _tax_rate_for(cat, label)
    gross = _to_gross(net, rate)

    item = {'id': _new_item_id(), 'label': label, 'amount': gross, 'category': cat, 'taxRate': rate}
    if vendor:
        item['vendor'] = vendor
    corp = report.get('corp') or _empty_bucket()
    corp['withdraws'] = (corp.get('withdraws') or []) + [item]
    report['corp'] = corp

    if not _save_with_undo(user_id, date_iso, before, report):
        return '保存に失敗しました。時間をおいてもう一度お試しください。'

    where = f'{vendor} / ' if vendor else ''
    rate_txt = '非課税' if rate == 0 else f'{rate}%'
    return '\n'.join([
        f'✅ {config.jp(date_iso)} {CATEGORY_LABEL[cat]}に追加',
        f'{where}{label}　税抜 {net:,}円（{rate_txt} → 税込 {gross:,}円）',
        '',
        _day_summary(report),
        '（間違えたら「取り消し」）',
    ])


def _set_sales(user_id, date_iso, rest):
    """金額が読み取れなければNone（普通のタスクとして扱わせる）。"""
    amount = _parse_amount(rest.strip())
    if amount is None:
        return None
    before = _report(date_iso)
    report = _report(date_iso) or _default_report(date_iso)
    cash = report.get('cash') or {**_empty_bucket(), 'sales': 0, 'salesNote': '', 'toBank': 0}
    cash['sales'] = amount
    report['cash'] = cash
    if not _save_with_undo(user_id, date_iso, before, report):
        return '保存に失敗しました。時間をおいてもう一度お試しください。'
    return '\n'.join([
        f'✅ {config.jp(date_iso)} の売上を {amount:,}円 にしました',
        '',
        _day_summary(report),
        '（間違えたら「取り消し」）',
    ])


def _add_shift(user_id, date_iso, rest):
    """時刻が2つ揃っていなければNone（「シフト表を作る」等をタスクとして扱わせる）。"""
    parts = rest.replace('〜', ' ').replace('~', ' ').replace('-', ' ').split()
    if len(parts) < 3:
        return None
    name_raw = parts[0]
    t_in, t_out = _parse_time(parts[1]), _parse_time(parts[2])
    if not t_in or not t_out:
        return None

    staff = _staff_list()
    member = next((s for s in staff if s['name'] == name_raw), None)
    if not member:
        member = next((s for s in staff if name_raw in s['name'] or s['name'].startswith(name_raw)), None)
    if not member:
        names = '／'.join(s['name'] for s in staff)
        return f'「{name_raw}」がスタッフ台帳に見つかりません。\n登録済み：{names}'

    before = _report(date_iso)
    report = _report(date_iso) or _default_report(date_iso)
    entry = {
        'id': _new_item_id(), 'staffName': member['name'],
        'clockIn': t_in, 'clockOut': t_out,
        'hourlyWage': member.get('hourlyWage', 1500), 'transport': member.get('transport', 0),
    }
    # 同じ人の同じ時間帯が既にあれば置き換える（二重登録の防止）
    shifts = [s for s in (report.get('shifts') or [])
              if not (s.get('staffName') == member['name'] and s.get('clockIn') == t_in)]
    shifts.append(entry)
    report['shifts'] = shifts
    report = _sync_labor(report)

    if not _save_with_undo(user_id, date_iso, before, report):
        return '保存に失敗しました。時間をおいてもう一度お試しください。'

    pay = _shift_pay(entry)
    total = sum(_shift_pay(s) for s in shifts)
    return '\n'.join([
        f'✅ {config.jp(date_iso)} シフト追加',
        f'{member["name"]}　{t_in}〜{t_out}　{pay:,}円',
        f'この日の人件費　{total:,}円',
        '',
        _day_summary(report),
        '（間違えたら「取り消し」）',
    ])


def _undo(user_id):
    snap = get_state(f'{UNDO_KEY}:{user_id}')
    if not snap:
        return '取り消せる操作がありません。'
    date_iso = snap.get('date')
    before = snap.get('before')
    if before is None:
        # もともと存在しなかった日 → 空の状態に戻す
        ok = _kv_set(f'report:{date_iso}', _default_report(date_iso))
    else:
        ok = _kv_set(f'report:{date_iso}', before)
    delete_state(f'{UNDO_KEY}:{user_id}')
    if not ok:
        return '取り消しに失敗しました。'
    restored = _report(date_iso) or _default_report(date_iso)
    return '\n'.join([
        f'↩️ {config.jp(date_iso)} の直前の入力を取り消しました',
        '',
        _day_summary(restored),
    ])


def _only_date(line):
    """「8/6」「8/6(木)」「8月6日」のように日付だけの行なら日付を返す。違えばNone。"""
    m = re.fullmatch(r'(\d{1,2})[/月](\d{1,2})日?(?:\([^)]*\))?', line)
    if not m:
        return None
    today = config.now_jst()
    mo, da = int(m.group(1)), int(m.group(2))
    year = today.year
    if today.month == 12 and mo == 1:
        year += 1
    elif today.month == 1 and mo == 12:
        year -= 1
    return f'{year:04d}-{mo:02d}-{da:02d}'


def _apply_one_line(user_id, date_iso, line):
    """1行を処理する。財務コマンドでなければNone。"""
    d, body = _resolve_date(line)
    # 行頭に日付が無ければ、テンプレートで指定された日付を使う
    if not re.match(r'^\d{1,2}[/月]\d{1,2}日?\s', line):
        d = date_iso

    m = re.match(r'^(売上|うりあげ)[\s:：]+([\s\S]+)$', body)
    if m:
        return _set_sales(user_id, d, m.group(2))

    m = re.match(r'^(シフト|出勤)[\s:：]+([\s\S]+)$', body)
    if m:
        return _add_shift(user_id, d, m.group(2))

    for cat, words in (
        ('ingredient', r'食材|食品|仕入れ?'),
        ('supplies', r'備品|消耗品'),
        ('other', r'経費|その他経費'),
    ):
        m = re.match(rf'^({words})[\s:：]+([\s\S]+)$', body)
        if m:
            return _add_expense_item(user_id, d, cat, m.group(2))
    return None


def handle_finance_input(user_id, text):
    """財務の入力コマンドを処理する。該当しなければ None を返して他のルーティングに任せる。

    テンプレートをそのまま送り返せるよう、複数行のメッセージにも対応する。
    ・日付だけの行（8/6）が現れたら、それ以降の行はその日付として扱う
    ・金額が空のままの行・見出し行・区切り線などは黙って読み飛ばす
    """
    t = _norm(text)
    if not t:
        return None

    if re.fullmatch(r'(取り消し|取消|とりけし|undo)', t, re.IGNORECASE):
        return _undo(user_id)

    if re.fullmatch(r'(テンプレ|テンプレート|入力フォーム|フォーム|入力)', t):
        return build_input_template()

    lines = [ln.strip() for ln in t.split('\n')]
    date_iso = config.today_iso()
    results = []
    touched = []

    for line in lines:
        if not line:
            continue
        d = _only_date(line)
        if d:
            date_iso = d
            continue
        res = _apply_one_line(user_id, date_iso, line)
        if res is not None:
            results.append(res)
            if date_iso not in touched:
                touched.append(date_iso)

    if not results:
        return None
    if len(results) == 1:
        return results[0]

    # 複数行まとめて入力された場合は、1行ずつの結果ではなく最終的な集計だけ返す
    ok = sum(1 for r in results if r.startswith('✅'))
    ng = [r for r in results if not r.startswith('✅')]
    out = [f'✅ {ok}件を登録しました']
    for date_iso in touched:
        rep = _report(date_iso)
        if rep:
            out.append(f'{config.jp(date_iso)}　{_day_summary(rep)}')
    if ng:
        out.append('')
        out.append('⚠️ 登録できなかったもの')
        out.extend(ng)
    out.append('')
    out.append('（間違えたら「取り消し」）')
    return '\n'.join(out)


def build_input_template(date_iso=None):
    """コピーして数字を書き入れるための最小限のひな形。説明と本体を別メッセージで返す。

    まとめて入力したいときはアプリ（entry_url）のほうが速いので、
    ここでは「LINEで済ませたい人向けの最小限」に絞っている。
    """
    date_iso = date_iso or config.today_iso()
    master = _item_master()

    def first(seq, fallback):
        return seq[0] if seq else fallback

    ing_vendor = first(master['vendors'].get('ingredient', []), '肉のハナマサ')

    guide = '\n'.join([
        f'📋 {config.jp(date_iso)} の入力ひな形',
        '',
        '下のメッセージをコピーして数字を入れ、',
        'そのまま送り返してください。',
        '・使わない行は消してOK',
        '・空欄のままの行は無視されます',
        '・行は増やせます',
        '',
        f'まとめて入れるならアプリのほうが速いです：\n{entry_url(date_iso)}',
    ])

    body_lines = [
        config.jp(date_iso),
        '売上 ',
        f'食材 {ing_vendor} お米 ',
        '経費 ATM手数料 ',
    ]
    return [guide, '\n'.join(body_lines)]


def finance_help():
    return '\n'.join([
        '💰 財務の入力',
        '',
        '▼ まとめて入力するとき',
        f'アプリが速いです：{entry_url(config.today_iso())}',
        '',
        '▼ 1件だけサッと入れるとき',
        '・売上 52000',
        '・食材 お米 1000',
        '・食材 肉のハナマサ お米 1000　← 仕入れ先も分ける場合',
        '・備品 シモジマ タレ瓶 500',
        '・経費 ATM手数料 110',
        '・シフト 都丸 10:00 14:30',
        '',
        '※ 金額は税抜で入れてください（品目ごとの税率で税込に直して保存します）',
        '※ 売上だけは税込で入れてください',
        '※ 昨日など別の日は先頭に日付：「8/5 売上 52000」',
        '※ 間違えたら「取り消し」で直前の1件を戻せます',
        '',
        '確認は「収支」「今月の収支」',
        APP_URL,
    ])


def build_finance_reminder(date_iso=None):
    """毎日16時のリマインド。未入力なら催促、入力済みなら現状を返す。"""
    date_iso = date_iso or config.today_iso()
    report = _report(date_iso)
    revenue, expense, profit, by_cat = _totals(report) if report else (0, 0, 0, {})
    has_shift = bool((report or {}).get('shifts'))

    lines = [f'📝 {config.jp(date_iso)} の入力確認']
    lines.append('')
    if not report or (revenue == 0 and expense == 0):
        lines.append('まだ何も入力されていません。')
    else:
        lines.append(f'売上　{_yen(revenue)}' + ('' if revenue else '　← 未入力'))
        lines.append(f'人件費　{by_cat.get("labor", 0):,}円' + ('' if has_shift else '　← シフト未入力'))
        lines.append(f'仕入・経費　{expense - by_cat.get("labor", 0):,}円')
        lines.append(f'当日損益　{_signed(profit)}')

    missing = []
    if not revenue:
        missing.append('売上')
    if not has_shift:
        missing.append('シフト')
    if missing:
        lines.append('')
        lines.append(f'未入力：{"・".join(missing)}')

    lines.append('')
    lines.append('▼ アプリで入力（この日が開きます）')
    lines.append(entry_url(date_iso))
    lines.append('')
    lines.append('▼ LINEで入力（1行でも複数行でもOK）')
    lines.append('　売上 41200')
    lines.append('　食材 肉のハナマサ お米 1000')
    lines.append('（書き方は「入力ヘルプ」）')
    return '\n'.join(lines)


def send_finance_reminder(push_text, get_users):
    """cronから呼ぶ。毎日16時の入力リマインド（本文＋テンプレートの2通）。"""
    body = build_finance_reminder()
    for uid in get_users():
        push_text(uid, body)
