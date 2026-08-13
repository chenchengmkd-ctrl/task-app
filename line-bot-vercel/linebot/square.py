"""Square（レジアプリ）の売上を財務アプリに取り込む。

SquareのPayments APIから1日分の決済を集計し、財務アプリの「本日現金売上」（cash.sales）に入れる。

注意している点：
- Squareで打った売上には現金もカードも含まれる。どちらがいくらかを必ず内訳で示してから取り込む
  （カードだけを見て「売上が足りない」と誤解しないため）。
- 返金（refunded_money）は差し引く。
- 自動取り込みは「まだ売上が入っていない日」だけ書き込む。手入力済みの日は上書きせず、
  金額が食い違っていればその旨だけ知らせる（人が入れた数字を機械が黙って消さない）。
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
        lines.append('「スクエア取込」でSquareの金額に上書きできます')
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


def sync_daily(push, get_users_fn):
    """毎晩の自動取り込み（cronから呼ばれる）。

    まだ売上が入っていない日だけ書き込む。手で入れた数字は上書きせず、
    食い違っているときだけ知らせる。
    """
    if not is_enabled():
        return
    date_iso = config.today_iso()
    s = summarize(date_iso)
    if s is None or s['total'] <= 0:
        return

    entered = finance_mod.sales_of(date_iso)
    users = get_users_fn()

    if not entered:
        if finance_mod.set_sales_amount(None, date_iso, s['total']) is None:
            return
        text = '\n'.join(
            [f'🧾 Square {config.jp(date_iso)} の売上を自動で取り込みました'] + _summary_lines(s)
            + ['', finance_mod.entry_url(date_iso)]
        )
    elif entered != s['total']:
        text = '\n'.join([
            f'⚠️ Square {config.jp(date_iso)} の売上とアプリの数字が違います',
            f'Square {s["total"]:,}円 ／ アプリ {entered:,}円（差 {s["total"] - entered:+,}円）',
            '',
            'Squareに合わせるなら「スクエア取込」',
        ])
    else:
        return

    for uid in users:
        push(uid, text)


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
