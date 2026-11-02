"""月次レポート用に、1か月分の数字を財務アプリのデータ（Supabase）から集める。

使い方:
    python3 monthly-report/fetch_month.py --store kanda --month 2026-11 > month.json

必要な環境変数:
    SUPABASE_URL, SUPABASE_ANON_KEY  （LINE bot と同じもの。読むだけで書き込みはしない）

読むデータ（どれも財務アプリ・LINE bot が毎日保存しているもの）:
    {prefix}report:YYYY-MM-DD  … 日次の売上・経費・シフト（金額は税込）
    {prefix}sales:YYYY-MM-DD   … Squareの出数・会計数（毎日の自動取り込みで保存）

計算のきまり（10月のレポートと同じ）:
    - 売上 = 現金売上 + 法人入金 + 個人入金（finance.py の _revenue と同じ）
    - 税抜 = 明細ごとに 税込 ÷ (1 + 税率)、合計してから1円未満切り捨て
    - 売上の税抜は、税込 ÷ 1.08（持ち帰り中心のため。店舗設定 sales_tax_rate で変更可）
    - 主原料の使用量換算は、店舗設定 main_ingredient.menu_rates（鰻重 特上1.5尾・上1尾・並0.5尾）
"""
import argparse
import calendar
import json
import os
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path
from urllib.parse import quote

import requests

HERE = Path(__file__).resolve().parent
WEEKDAYS = '月火水木金土日'
CATEGORY_LABEL = {
    'ingredient': '食材', 'supplies': '備品', 'labor': '人件費',
    'rent': '家賃', 'utility': '光熱費', 'other': 'その他',
}
DEFAULT_TAX_RATE = {'ingredient': 8, 'supplies': 10, 'labor': 0, 'rent': 10, 'utility': 10, 'other': 10}


def load_store(store_id):
    path = HERE / 'stores' / f'{store_id}.json'
    if not path.exists():
        sys.exit(f'店舗設定がありません: {path}')
    return json.loads(path.read_text(encoding='utf-8'))


def kv_month(store, kind, month):
    url_base = os.environ.get('SUPABASE_URL', '').strip()
    key = os.environ.get('SUPABASE_ANON_KEY', '').strip()
    if not url_base or not key:
        sys.exit('SUPABASE_URL と SUPABASE_ANON_KEY を環境変数に設定してください。')
    like = f"{store['kv_prefix']}{kind}:{month}"
    url = f"{url_base}/rest/v1/{store['kv_table']}?key=like.{quote(like)}*&select=key,value"
    res = requests.get(url, headers={'apikey': key, 'Authorization': f'Bearer {key}',
                                     'Accept': 'application/json'}, timeout=30)
    res.raise_for_status()
    out = {}
    for row in res.json():
        d = row['key'].rsplit(':', 1)[-1]
        if row.get('value'):
            out[d] = row['value']
    return out


def net_of(amount, rate):
    return amount * 100 / (100 + (rate or 0))


def shift_minutes(s):
    try:
        ih, im = [int(x) for x in (s.get('clockIn') or '').split(':')]
        oh, om = [int(x) for x in (s.get('clockOut') or '').split(':')]
    except ValueError:
        return 0
    return max(0, (oh * 60 + om) - (ih * 60 + im))


def shift_pay(s):
    mins = shift_minutes(s)
    if mins <= 0:
        return 0
    return int(round(mins / 60 * int(s.get('hourlyWage') or 0) + int(s.get('transport') or 0)))


def revenue_of(r):
    def items(xs):
        return sum(int(i.get('amount') or 0) for i in (xs or []))
    cash, corp, pers = r.get('cash') or {}, r.get('corp') or {}, r.get('pers') or {}
    return int(cash.get('sales') or 0) + items(corp.get('deposits')) + items(pers.get('deposits'))


def ingredient_rate(name, ing):
    if ing['menu_match'] not in name:
        return None
    for r in ing['menu_rates']:
        if r['match'] in name:
            return r
    return None


def build(store, month):
    y, m = int(month[:4]), int(month[5:7])
    days_in_month = calendar.monthrange(y, m)[1]
    reports = kv_month(store, 'report', month)
    sales = kv_month(store, 'sales', month)
    sales_tax = store.get('sales_tax_rate', 8)
    ing = store.get('main_ingredient')  # 主原料が無い業態では省ける

    daily = []
    cat_gross = defaultdict(int)
    cat_net = defaultdict(float)
    lines = defaultdict(lambda: {'gross': 0, 'net': 0.0, 'count': 0, 'dates': []})
    purchases = []
    staff = defaultdict(lambda: {'days': set(), 'minutes': 0, 'pay': 0})
    menu = defaultdict(lambda: {'qty': 0, 'amount': 0})

    for day in range(1, days_in_month + 1):
        d = f'{month}-{day:02d}'
        r = reports.get(d) or {}
        s = sales.get(d) or {}
        rev = revenue_of(r) if r else 0
        exp = 0
        for bucket in ('corp', 'pers', 'cash'):
            for it in ((r.get(bucket) or {}).get('withdraws') or []):
                cat = it.get('category') or 'other'
                amt = int(it.get('amount') or 0)
                rate = it.get('taxRate', DEFAULT_TAX_RATE.get(cat, 10))
                exp += amt
                cat_gross[cat] += amt
                cat_net[cat] += net_of(amt, rate)
                label = (it.get('label') or '（品名なし）').strip()
                vendor = (it.get('vendor') or '').strip()
                k = (cat, label, vendor)
                lines[k]['gross'] += amt
                lines[k]['net'] += net_of(amt, rate)
                lines[k]['count'] += 1
                lines[k]['dates'].append(d)
                if ing and cat == 'ingredient' and any(w in label + vendor for w in ing['purchase_match']):
                    purchases.append({'date': d, 'label': label, 'vendor': vendor, 'amount': amt, 'taxRate': rate})
        for sh in (r.get('shifts') or []):
            st = staff[sh.get('staffName') or '（名前なし）']
            st['days'].add(d)
            st['minutes'] += shift_minutes(sh)
            st['pay'] += shift_pay(sh)
        tails = 0.0
        for it in (s.get('items') or []):
            menu[it['name']]['qty'] += int(it.get('qty') or 0)
            menu[it['name']]['amount'] += int(it.get('amount') or 0)
            rr = ingredient_rate(it['name'], ing) if ing else None
            if rr:
                tails += int(it.get('qty') or 0) * rr['rate']
        wd = WEEKDAYS[date(y, m, day).weekday()]
        daily.append({'date': d, 'weekday': wd, 'open': rev > 0, 'revenue': rev, 'expense': exp,
                      'checkouts': int(s.get('customers') or 0), 'ingredient_use_by_menu': round(tails, 1),
                      'note': (r.get('note') or '').strip()})

    open_days = [x for x in daily if x['open']]
    revenue = sum(x['revenue'] for x in daily)
    revenue_net = int(revenue / (1 + sales_tax / 100))
    expense = sum(cat_gross.values())
    expense_net = int(sum(cat_net.values()))
    by_wd = {}
    for wd in WEEKDAYS:
        xs = [x for x in open_days if x['weekday'] == wd]
        if xs:
            by_wd[wd] = {'days': len(xs), 'avg_revenue': round(sum(x['revenue'] for x in xs) / len(xs)),
                         'avg_checkouts': round(sum(x['checkouts'] for x in xs) / len(xs), 1)}
    top = sorted(open_days, key=lambda x: -x['revenue'])
    p_total = sum(p['amount'] for p in purchases)

    return {
        'store': store['id'], 'store_name': store['name'], 'month': month,
        'open_days': len(open_days),
        'revenue': {'gross': revenue, 'net': revenue_net,
                    'per_day': round(revenue / len(open_days)) if open_days else 0,
                    'best': top[:3], 'worst': top[-3:][::-1]},
        'checkouts': sum(x['checkouts'] for x in daily),
        'expenses': {
            'gross': expense, 'net': expense_net,
            'by_category': {CATEGORY_LABEL.get(c, c): {'gross': cat_gross[c], 'net': int(cat_net[c]),
                                                        'ratio_of_sales_net': round(cat_net[c] / revenue_net * 100, 1) if revenue_net else None}
                            for c in cat_gross},
            'lines': sorted([{'category': CATEGORY_LABEL.get(k[0], k[0]), 'label': k[1], 'vendor': k[2],
                              'gross': v['gross'], 'net': int(v['net']), 'count': v['count'], 'dates': v['dates']}
                             for k, v in lines.items()], key=lambda x: (x['category'], -x['gross'])),
        },
        'profit': {'gross': revenue - expense, 'net': revenue_net - expense_net},
        'staff_shifts': {n: {'days': len(v['days']), 'hours': round(v['minutes'] / 60, 1), 'pay': v['pay']}
                         for n, v in staff.items()},
        'main_ingredient': None if not ing else {
            'name': ing['name'], 'unit': ing['unit'],
            'purchases': purchases, 'purchase_total': p_total,
            'purchase_units_estimate': len(purchases) * ing.get('units_per_purchase', 0),
            'use_by_menu': round(sum(x['ingredient_use_by_menu'] for x in daily), 1),
        },
        'menu': sorted([{'name': k, **v} for k, v in menu.items()], key=lambda x: -x['amount']),
        'by_weekday': by_wd,
        'daily': daily,
        'missing_days': {
            'no_finance_report': [x['date'] for x in daily if x['date'] not in reports],
            'open_but_no_square_detail': [x['date'] for x in open_days if x['date'] not in sales],
        },
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--store', required=True)
    ap.add_argument('--month', required=True, help='YYYY-MM')
    args = ap.parse_args()
    data = build(load_store(args.store), args.month)
    json.dump(data, sys.stdout, ensure_ascii=False, indent=1)
    print()


if __name__ == '__main__':
    main()
