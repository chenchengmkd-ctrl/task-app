"""日報アプリ（nippo.html）用。Squareの売上と、会計ごとの明細を返す。

iPadの日報アプリが会計のたびに呼び、
- 本日売上（Squareの決済合計。返金は差し引き済み）
- 会計ごとの時刻と注文明細（来店経緯を会計単位でタップするため）
を受け取る。売上は日報に手で書かず、Squareを正とする。
"""
from datetime import datetime, timedelta, timezone

from . import square

JST = timezone(timedelta(hours=9))


def _jst_hm(rfc3339):
    try:
        dt = datetime.fromisoformat((rfc3339 or '').replace('Z', '+00:00'))
    except ValueError:
        return ''
    return dt.astimezone(JST).strftime('%H:%M')


def _items(order):
    out = []
    for li in (order.get('line_items') or []):
        name = (li.get('name') or '').strip()
        variation = (li.get('variation_name') or '').strip()
        if variation and variation.lower() != 'regular':
            name = f'{name}（{variation}）'
        try:
            qty = int(float(li.get('quantity') or 0))
        except (TypeError, ValueError):
            qty = 0
        if name and qty:
            out.append({'name': name, 'qty': qty})
    return out


def day_sales(date_iso):
    if not square.is_enabled():
        return {'ok': False, 'error': 'Squareが未設定です'}
    s = square.summarize(date_iso)
    orders = square.fetch_orders(date_iso)
    if s is None or orders is None:
        return {'ok': False, 'error': 'Squareからデータを取得できませんでした'}
    return {
        'ok': True,
        'date': date_iso,
        'total': s['total'],
        'count': s['count'],
        'orders': [
            {
                'id': o.get('id'),
                'time': _jst_hm(o.get('closed_at') or o.get('created_at')),
                'total': square._amount(o.get('total_money')),
                'items': _items(o),
            }
            for o in orders
        ],
    }
