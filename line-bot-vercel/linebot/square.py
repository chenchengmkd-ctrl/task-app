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
from datetime import datetime, timedelta
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

    # 出数・客数・現金内訳もあわせて取り込む（アプリの分析タブ・LINE通知・資金繰り予想の全部で使う）。
    # 通知するかどうかに関わらず、日々の記録は毎回残す（資金繰り予想の実績データが途切れないように）
    detail = None
    try:
        detail = store_sales_detail(date_iso)
    except Exception as e:
        print('square sales detail sync error:', e)
    try:
        store_cash_split(date_iso, s)
    except Exception as e:
        print('square cash split sync error:', e)

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

    try:
        cf = cashflow_forecast(date_iso)
        lines += [''] + _cashflow_lines(date_iso, cf)
    except Exception as e:
        print('square cashflow forecast error:', e)

    # 出数（売れた個数の上位）を通知にも載せる。全件だと長くなるので上位5件まで
    if detail and detail.get('items'):
        lines += ['', f'🍱 出数（客数 {detail["customers"]}組 ／ 客単価 {detail["perCustomer"]:,}円）']
        for i in detail['items'][:5]:
            lines.append(f'　{i["name"]}　{i["qty"]}個')
        if len(detail['items']) > 5:
            lines.append(f'　…ほか{len(detail["items"]) - 5}品（「出数」で全件見れます）')
        usage_lines = _usage_lines(detail['items'])
        if usage_lines:
            lines += [''] + usage_lines
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


def store_sales_detail(date_iso):
    """その日の出数・客数をSupabaseに保存する（キー: birdmen:sales:YYYY-MM-DD）。

    アプリの「分析」タブがこれを読む。売上金額そのものは日次データ（report:）側が正なので、
    ここは商品ごとの個数・客数・時間帯といった、Squareにしか無い情報を持つ。
    """
    detail = sales_detail(date_iso)
    if detail is None:
        return None
    if detail['customers'] == 0:
        return detail  # 営業していない日は保存しない（空データでグラフを汚さない）
    finance_mod.kv_set(f'sales:{date_iso}', detail)
    return detail


def backfill_sales_detail(month):
    """指定月（YYYY-MM）の出数・客数・現金内訳をまとめて取り込む。過去分を後から入れる用。"""
    import calendar as _cal
    y, m = int(month[:4]), int(month[5:7])
    days = _cal.monthrange(y, m)[1]
    today = config.today_iso()
    saved, customers, total = 0, 0, 0
    for d in range(1, days + 1):
        date_iso = f'{y:04d}-{m:02d}-{d:02d}'
        if date_iso > today:
            break
        detail = store_sales_detail(date_iso)
        try:
            store_cash_split(date_iso)
        except Exception as e:
            print('square cash split backfill error:', date_iso, e)
        if detail and detail['customers'] > 0:
            saved += 1
            customers += detail['customers']
            total += detail['total']
    return {'month': month, 'days': saved, 'customers': customers, 'total': total}


# ===== 資金繰り（現金・現金以外の内訳と、次回金曜振込の予想） =====
# Squareの入金は「木曜0:00〜翌水曜23:59に発生した売上（現金以外）」が「次の金曜日」にまとめて
# 銀行口座へ振り込まれる、というSquare公式の精算サイクルに基づく。現金は当日その場で受け取っている
# ので振込対象に含めない。

def store_cash_split(date_iso, s=None):
    """その日の現金／現金以外の内訳を保存する（birdmen:cashflow:YYYY-MM-DD）。
    sを渡せばPayments APIを呼び直さない（sync_daily側で既に取得済みのため）。
    """
    s = s or summarize(date_iso)
    if s is None:
        return None
    cash = s['by_source'].get('CASH', 0)
    record = {'date': date_iso, 'cash': cash, 'noncash': s['total'] - cash, 'total': s['total']}
    finance_mod.kv_set(f'cashflow:{date_iso}', record)
    return record


def _cashflow_month(month):
    """指定月（YYYY-MM）のcashflowレコードを日付順で返す。"""
    records = [v for _, v in finance_mod.kv_list(f'cashflow:{month}')]
    return sorted(records, key=lambda r: r.get('date', ''))


def _mtd_cash(date_iso):
    """その月の1日から指定日までの現金売上累計。"""
    month = date_iso[:7]
    return sum(int(r.get('cash') or 0) for r in _cashflow_month(month) if r.get('date') <= date_iso)


def _next_friday_window(today):
    """今日を含む「次の金曜振込」対象の精算期間（木曜0:00〜翌水曜23:59）を返す。"""
    days_until_friday = (4 - today.weekday()) % 7  # 月曜=0 … 金曜=4
    next_friday = today + timedelta(days=days_until_friday)
    window_end = next_friday - timedelta(days=2)     # 水曜
    window_start = window_end - timedelta(days=6)    # 木曜
    return window_start, window_end, next_friday


def cashflow_forecast(date_iso=None):
    """次回金曜に振り込まれる「現金以外」の金額を、精算期間の実績＋残り日数の平均で見積もる。
    期間がすでに終わっていれば実績確定額をそのまま返す。
    """
    date_iso = date_iso or config.today_iso()
    today = datetime.strptime(date_iso, '%Y-%m-%d').date()
    window_start, window_end, next_friday = _next_friday_window(today)
    ws_iso, we_iso = window_start.strftime('%Y-%m-%d'), window_end.strftime('%Y-%m-%d')

    records = {}
    for m in {window_start.strftime('%Y-%m'), window_end.strftime('%Y-%m')}:
        for r in _cashflow_month(m):
            records[r['date']] = r
    in_window = [r for d, r in records.items() if ws_iso <= d <= we_iso]
    actual_noncash = sum(int(r.get('noncash') or 0) for r in in_window)
    days_with_data = len(in_window)

    finished = today > window_end
    projected = actual_noncash
    if not finished and days_with_data > 0:
        remaining_days = (window_end - today).days
        projected = round(actual_noncash + (actual_noncash / days_with_data) * remaining_days)

    return {
        'nextFriday': next_friday.strftime('%Y-%m-%d'),
        'windowStart': ws_iso, 'windowEnd': we_iso,
        'actualNoncash': actual_noncash, 'projectedNoncash': projected,
        'finished': finished, 'daysWithData': days_with_data,
        'mtdCash': _mtd_cash(date_iso),
    }


def _cashflow_lines(date_iso, cf):
    friday_label = '本日振込予定' if cf['nextFriday'] == date_iso else f'次回振込予定（{config.jp(cf["nextFriday"])}）'
    amount_label = '確定' if cf['finished'] else '目安'
    return [
        '💴 資金繰り',
        f'現金売上（{int(date_iso[5:7])}月累計）　{cf["mtdCash"]:,}円',
        f'{friday_label}・現金以外　約{cf["projectedNoncash"]:,}円（{amount_label}）',
    ]


def build_cashflow_report(date_iso=None):
    """資金繰りだけを見る（「資金繰り予想」コマンド）。自動通知を待たずに確認する用。"""
    if not is_enabled():
        return 'Squareの連携がまだ設定されていません。'
    date_iso = date_iso or config.today_iso()
    s = summarize(date_iso)
    if s is None:
        return 'Squareからデータを取得できませんでした。'
    store_cash_split(date_iso, s)
    cf = cashflow_forecast(date_iso)
    lines = [f'💴 {config.jp(date_iso)} 時点の資金繰り', ''] + _cashflow_lines(date_iso, cf)[1:]
    lines.append('')
    lines.append(f'対象期間　{config.jp(cf["windowStart"])}〜{config.jp(cf["windowEnd"])}（{cf["daysWithData"]}日分のデータ）')
    return '\n'.join(lines)


# ===== 鰻・ご飯の使用量（出数から逆算した目安） =====
# ユーザー指定の換算：特上=1.5尾／上=1尾／並=0.5尾、ご飯は1食250g。
# 対象は品名に「鰻重」を含むものだけ（鰻おにぎり等は換算比が不明なため含めない）。
RICE_G_PER_SERVING = 250


def _unagi_usage(items):
    tails = 0.0
    servings = 0
    for i in items:
        name = i.get('name') or ''
        if '鰻重' not in name:
            continue
        qty = int(i.get('qty') or 0)
        if '特' in name:
            rate = 1.5
        elif '並' in name:
            rate = 0.5
        else:
            rate = 1.0  # 上
        tails += qty * rate
        servings += qty
    return {'tails': round(tails, 1), 'servings': servings, 'riceKg': round(servings * RICE_G_PER_SERVING / 1000, 2)}


def _usage_lines(items):
    usage = _unagi_usage(items)
    if usage['servings'] == 0:
        return []
    return [
        '🐟 鰻・ご飯の使用量（本日分の目安）',
        f'鰻　約{usage["tails"]:.1f}尾',
        f'ご飯　約{usage["riceKg"]:.2f}kg',
        '（鰻重系の出数から算出：特上1.5尾／上1尾／並0.5尾、ご飯は1食250g換算）',
    ]


def build_items_report(date_iso=None):
    """その日の出数をLINEで見る（「出数」コマンド）。取り込みも同時に行う。"""
    if not is_enabled():
        return 'Squareの連携がまだ設定されていません。'
    date_iso = date_iso or config.today_iso()
    detail = store_sales_detail(date_iso)
    if detail is None:
        return 'Squareから明細を取得できませんでした。'
    if detail['customers'] == 0:
        return f'Square {config.jp(date_iso)} の会計はまだありません。'

    lines = [
        f'🍱 {config.jp(date_iso)} の出数',
        f'客数 {detail["customers"]}組 ／ 客単価 {detail["perCustomer"]:,}円',
        '',
    ]
    for i in detail['items'][:15]:
        lines.append(f'{i["name"]}　{i["qty"]}個　{i["amount"]:,}円')
    if len(detail['items']) > 15:
        lines.append(f'…ほか{len(detail["items"]) - 15}品')
    lines += ['', f'合計 {detail["total"]:,}円（税込）']
    return '\n'.join(lines)


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
