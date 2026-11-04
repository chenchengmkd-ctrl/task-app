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


TAKEOUT_WORDS = ('テイクアウト', '持ち帰り', '持帰', 'ﾃｲｸｱｳﾄ', 'to go', 'togo', 'take out', 'takeout', 'take-out')


def _is_takeout(order):
    """Squareの会計がテイクアウトかどうか。
    商品名・バリエーション名・オプション（モディファイア）・伝票名に「テイクアウト」「持ち帰り」などが入っているか、
    受け取り方法（fulfillment）が店頭受け取り・配達なら True。"""
    texts = [order.get('ticket_name') or '']
    for li in (order.get('line_items') or []):
        texts += [li.get('name') or '', li.get('variation_name') or '']
        texts += [m.get('name') or '' for m in (li.get('modifiers') or [])]
    texts += [sc.get('name') or '' for sc in (order.get('service_charges') or [])]
    blob = ' '.join(texts).lower()
    if any(w in blob for w in TAKEOUT_WORDS):
        return True
    return any((f.get('type') or '') in ('PICKUP', 'DELIVERY') for f in (order.get('fulfillments') or []))


def day_sales(date_iso):
    if not square.is_enabled():
        return {'ok': False, 'error': 'Squareが未設定です'}
    s = square.summarize(date_iso)
    orders = square.fetch_orders(date_iso)
    if s is None or orders is None:
        return {'ok': False, 'error': 'Squareからデータを取得できませんでした'}
    m = square.month_total(date_iso)
    return {
        'ok': True,
        'date': date_iso,
        'total': s['total'],
        'month_total': m['total'] if m else None,  # その月の1日からこの日までの合計（累計売上に使う）
        'count': s['count'],
        'orders': [
            {
                'id': o.get('id'),
                'time': _jst_hm(o.get('closed_at') or o.get('created_at')),
                'total': square._amount(o.get('total_money')),
                'items': _items(o),
                'takeout': _is_takeout(o),
            }
            for o in orders
        ],
    }
