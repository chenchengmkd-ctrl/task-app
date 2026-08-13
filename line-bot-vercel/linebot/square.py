"""Square（レジアプリ）の売上を財務アプリに取り込む。

SquareのPayments APIから1日分の決済を集計し、財務アプリの「本日現金売上」（cash.sales）に入れる。

注意している点：
- Squareで打った売上には現金もカードも含まれる。どちらがいくらかを必ず内訳で示してから取り込む
  （カードだけを見て「売上が足りない」と誤解しないため）。
- 返金（refunded_money）は差し引く。
- 売上は手入力せずSquareを正とする運用なので、自動取り込みは毎回上書きする。
  ただし黙って書き換えないよう、既に入っていた金額と違うときはその旨も知らせる。
- 取り込みは15時（cron）に行い、そのあとに立った売上を拾うため22時のレポート直前にも
  通知なしでもう一度同期する。
- 金額は税込。アプリ側の cash.sales も税込なのでそのまま入る。
"""
import json
from urllib.parse import urlencode

import requests

from . import config
from . import finance as finance_mod

API_BASE = 'https://connect.squareup.com'
# 固定しておく（新しい版が出ても勝手に挙動が変わらないように）
API_VERSION = '2025-06-18'

# 売上として数える決済の状態。APPROVEDは与信のみで未確定なので含めない
COUNT_STATUS = ('COMPLETED',)

SOURCE_LABEL = {
    'CARD': 'カード',
    'CASH': '現金',
    'BANK_ACCOUNT': '口座振替',
    'WALLET': '電子マネー・QR',
    'BUY_NOW_PAY_LATER': '後払い',
    'SQUARE_ACCOUNT': 'Square残高',
    'EXTERNAL': 'その他',
}


def is_enabled():
    return bool(config.SQUARE_ACCESS_TOKEN)


def _headers():
    return {
        'Authorization': f'Bearer {config.SQUARE_ACCESS_TOKEN}',
        'Square-Version': API_VERSION,
        'Content-Type': 'application/json',
    }


def _get(path, params=None):
    url = f'{API_BASE}{path}'
    if params:
        url = f'{url}?{urlencode(params)}'
    try:
        res = requests.get(url, headers=_headers(), timeout=25)
    except requests.RequestException as e:
        print('square request failed', e)
        return None
    if res.status_code != 200:
        print('square api error', res.status_code, res.text[:300])
        return None
    try:
        return res.json()
    except ValueError:
        return None


def list_locations():
    """店舗の一覧。設定確認用（LOCATION_IDが分からないときに使う）。"""
    data = _get('/v2/locations')
    if not data:
        return []
    return [
        {'id': l.get('id'), 'name': l.get('name'), 'currency': l.get('currency'), 'status': l.get('status')}
        for l in (data.get('locations') or [])
    ]


def _location_id():
    """設定されていればそれを、無ければ有効な店舗の1件目を使う。"""
    if config.SQUARE_LOCATION_ID:
        return config.SQUARE_LOCATION_ID
    for loc in list_locations():
        if loc.get('status') == 'ACTIVE':
            return loc['id']
    return None


def _day_range(date_iso):
    """JSTのその日の始まりと終わり（RFC3339）。"""
    return f'{date_iso}T00:00:00+09:00', f'{date_iso}T23:59:59+09:00'


def _amount(money):
    """Money型から金額を取り出す。円は最小単位＝円なのでそのまま使える。"""
    if not isinstance(money, dict):
        return 0
    try:
        return int(money.get('amount') or 0)
    except (TypeError, ValueError):
        return 0


def fetch_payments(date_iso, location_id=None):
    """指定日の決済を全件取得する（ページングを最後までたどる）。失敗時はNone。

    location_id を省略すると設定済み（またはACTIVEな最初の1件）を使う。
    """
    location_id = location_id or _location_id()
    if not location_id:
        return None
    begin, end = _day_range(date_iso)
    payments, cursor = [], None
    for _ in range(20):  # 念のための上限（1日2000件まで）
        params = {
            'begin_time': begin,
            'end_time': end,
            'location_id': location_id,
            'limit': 100,
            'sort_order': 'ASC',
        }
        if cursor:
            params['cursor'] = cursor
        data = _get('/v2/payments', params)
        if data is None:
            return None
        payments.extend(data.get('payments') or [])
        cursor = data.get('cursor')
        if not cursor:
            break
    return payments


def summarize(date_iso, location_id=None):
    """1日分の売上を集計する。戻り値 {total, count, by_source, refunded}。取得できなければNone。"""
    payments = fetch_payments(date_iso, location_id)
    if payments is None:
        return None

    total, refunded, count = 0, 0, 0
    by_source = {}
    for p in payments:
        if (p.get('status') or '').upper() not in COUNT_STATUS:
            continue
        gross = _amount(p.get('amount_money'))
        back = _amount(p.get('refunded_money'))
        net = gross - back
        total += net
        refunded += back
        count += 1
        src = (p.get('source_type') or 'EXTERNAL').upper()
        by_source[src] = by_source.get(src, 0) + net

    return {'date': date_iso, 'total': total, 'count': count, 'by_source': by_source, 'refunded': refunded}


def _summary_lines(s):
    lines = [f'合計 {s["total"]:,}円（{s["count"]}件）']
    for src, amount in sorted(s['by_source'].items(), key=lambda kv: -kv[1]):
        lines.append(f'　{SOURCE_LABEL.get(src, src)} {amount:,}円')
    if s['refunded']:
        lines.append(f'　※返金 -{s["refunded"]:,}円を差し引き済み')
    return lines


def build_summary(date_iso=None):
    """Squareの売上を見るだけ（書き込まない）。LINEの「スクエア」コマンド用。"""
    if not is_enabled():
        return 'Squareの連携がまだ設定されていません（SQUARE_ACCESS_TOKEN 未設定）。'
    date_iso = date_iso or config.today_iso()
    s = summarize(date_iso)
    if s is None:
        return 'Squareからデータを取得できませんでした。アクセストークンの設定を確認してください。'

    entered = finance_mod.sales_of(date_iso)
    lines = [f'🧾 Square {config.jp(date_iso)} の売上'] + _summary_lines(s) + ['']
    if entered == s['total']:
        lines.append(f'アプリの売上と一致しています（{entered:,}円）')
    elif entered:
        lines.append(f'⚠️ アプリには {entered:,}円 が入っています（差 {s["total"] - entered:+,}円）')
        lines.append('「スクエア取込」で今すぐ合わせられます（15時に自動でも合います）')
    else:
        lines.append('アプリにはまだ売上が入っていません')
        lines.append('「スクエア取込」で取り込めます')
    return '\n'.join(lines)


def import_day(user_id, date_iso=None, overwrite=True):
    """Squareの売上を財務アプリに書き込む。LINEの「スクエア取込」コマンド用。"""
    if not is_enabled():
        return 'Squareの連携がまだ設定されていません（SQUARE_ACCESS_TOKEN 未設定）。'
    date_iso = date_iso or config.today_iso()
    s = summarize(date_iso)
    if s is None:
        return 'Squareからデータを取得できませんでした。アクセストークンの設定を確認してください。'
    if s['total'] <= 0:
        return f'Square {config.jp(date_iso)} の売上は0円でした。取り込みは行いません。'

    entered = finance_mod.sales_of(date_iso)
    if entered and not overwrite:
        return f'{config.jp(date_iso)} には既に {entered:,}円 が入っています。上書きするなら「スクエア取込」。'

    result = finance_mod.set_sales_amount(user_id, date_iso, s['total'])
    if result is None:
        return '保存に失敗しました。時間をおいてもう一度お試しください。'

    lines = [f'✅ Square {config.jp(date_iso)} の売上を取り込みました'] + _summary_lines(s)
    if entered and entered != s['total']:
        lines.append(f'（{entered:,}円 から上書きしました）')
    lines += ['', result, '（間違えたら「取り消し」）']
    return '\n'.join(lines)


def sync_daily(push, get_users_fn, notify=True):
    """自動取り込み（cronから呼ばれる）。

    売上はSquareを正とし、手入力を待たずに毎回上書きする（売上は手で入れない運用のため）。
    ただし黙って書き換えると気づけないので、既に入っていた数字と違うときはその旨も知らせる。
    notify=False のときは書き込むだけで通知しない（22時レポートの直前同期など）。
    """
    if not is_enabled():
        return None
    date_iso = config.today_iso()
    s = summarize(date_iso)
    if s is None or s['total'] <= 0:
        return None

    entered = finance_mod.sales_of(date_iso)
    if entered == s['total']:
        return s  # 既に一致しているので書き込みも通知も不要

    if finance_mod.set_sales_amount(None, date_iso, s['total']) is None:
        return None
    if not notify:
        return s

    head = f'🧾 Square {config.jp(date_iso)} の売上を取り込みました'
    lines = [head] + _summary_lines(s)
    if entered:
        lines.append(f'（アプリに入っていた {entered:,}円 から更新）')
    lines += ['', finance_mod.entry_url(date_iso)]
    text = '\n'.join(lines)

    for uid in get_users_fn():
        push(uid, text)
    return s


def _post(path, body):
    try:
        res = requests.post(f'{API_BASE}{path}', headers=_headers(), data=json.dumps(body), timeout=25)
    except requests.RequestException as e:
        print('square post failed', e)
        return None
    if res.status_code != 200:
        print('square api error', res.status_code, res.text[:300])
        return None
    try:
        return res.json()
    except ValueError:
        return None


def fetch_orders(date_iso, location_id=None):
    """指定日の注文を明細つきで取得する。出数・客数を出すのに使う。失敗時はNone。"""
    location_id = location_id or _location_id()
    if not location_id:
        return None
    begin, end = _day_range(date_iso)
    orders, cursor = [], None
    for _ in range(20):
        body = {
            'location_ids': [location_id],
            'limit': 500,
            'query': {
                'filter': {
                    'date_time_filter': {'closed_at': {'start_at': begin, 'end_at': end}},
                    'state_filter': {'states': ['COMPLETED']},
                },
                'sort': {'sort_field': 'CLOSED_AT', 'sort_order': 'ASC'},
            },
        }
        if cursor:
            body['cursor'] = cursor
        data = _post('/v2/orders/search', body)
        if data is None:
            return None
        orders.extend(data.get('orders') or [])
        cursor = data.get('cursor')
        if not cursor:
            break
    return orders


def sales_detail(date_iso, location_id=None):
    """1日の「出数（商品ごとの個数・金額）」と「客数（会計数）」を集計する。

    客数は会計（注文）の件数。1グループ1会計なので厳密な来客人数ではなく組数に近いが、
    屋台の持ち帰り中心なら実用上これで足りる。
    金額は税込（Squareのtotal_money）。
    """
    orders = fetch_orders(date_iso, location_id)
    if orders is None:
        return None

    items = {}
    total = 0
    by_hour = {}
    for o in orders:
        total += _amount(o.get('total_money'))
        closed = o.get('closed_at') or o.get('created_at') or ''
        # RFC3339のUTC表記なのでJSTへ直してから時間帯に振り分ける
        if len(closed) >= 13:
            try:
                hour = (int(closed[11:13]) + 9) % 24
                by_hour[hour] = by_hour.get(hour, 0) + 1
            except ValueError:
                pass
        for li in (o.get('line_items') or []):
            name = (li.get('name') or '（名称なし）').strip()
            variation = (li.get('variation_name') or '').strip()
            key = f'{name}（{variation}）' if variation and variation.lower() != 'regular' else name
            try:
                qty = int(float(li.get('quantity') or 0))
            except (TypeError, ValueError):
                qty = 0
            cur = items.setdefault(key, {'name': key, 'qty': 0, 'amount': 0})
            cur['qty'] += qty
            cur['amount'] += _amount(li.get('total_money'))

    ranked = sorted(items.values(), key=lambda i: -i['amount'])
    customers = len(orders)
    return {
        'date': date_iso,
        'customers': customers,
        'total': total,
        'perCustomer': round(total / customers) if customers else 0,
        'items': ranked,
        'byHour': by_hour,
    }


def diagnose():
    """設定確認用。トークンが有効か・店舗が見えるかだけ返す（/api/health から呼ぶ）。"""
    if not is_enabled():
        return {'enabled': False}
    locations = list_locations()
    if not locations:
        return {'enabled': True, 'ok': False, 'error': 'locations取得に失敗（トークンを確認）'}
    today = summarize(config.today_iso())
    # 店舗が複数ある場合、SQUARE_LOCATION_ID未設定だとどれが選ばれるか分かりにくいので、
    # 直近7日の売上件数を店舗ごとに出す（実際に売上が立っている店舗を見分けるため）
    from datetime import timedelta
    per_location = []
    if len(locations) > 1:
        for loc in locations:
            if loc.get('status') != 'ACTIVE':
                continue
            week_count, week_total = 0, 0
            for i in range(7):
                d = config.iso_of_date(config.now_jst() - timedelta(days=i))
                s = summarize(d, loc['id'])
                if s:
                    week_count += s['count']
                    week_total += s['total']
            per_location.append({'id': loc['id'], 'name': loc['name'], 'last7days_count': week_count, 'last7days_total': week_total})
    return {
        'enabled': True,
        'ok': True,
        'locations': locations,
        'location_used': _location_id(),
        'today': today,
        'per_location_last7days': per_location,
    }
