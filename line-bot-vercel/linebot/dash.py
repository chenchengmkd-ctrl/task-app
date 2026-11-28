"""毎朝見るダッシュボード（dash.html）用。今日のGoogleカレンダーの予定、店ごとの売上（昨日・今月）、
朝のメールのまとめ（別の仕組みが dash:mail に置いたもの）を1回でまとめて返す。見るだけで、何も書き換えない。

- 売上はレジごとに取り方が違うので、1つ失敗してもほかは返す
- 売上は数十秒かかることがあるため、日付ごとに20分だけ使い回す
"""
import hashlib
import time
from datetime import date, timedelta

from . import config
from urllib.parse import quote

from .supabase_client import get_state, get_supabase, set_state

OWNER_KEY = 'dash:owner'
SALES_TTL_SEC = 20 * 60


def check_key(key):
    """合言葉の確認。DASH_TOKEN が設定されていればそれと比べる。なければ、最初に使われた合言葉を持ち主として覚え、
    以後はその合言葉だけを通す。→ (ok, registered_now)"""
    key = str(key or '')
    if len(key) < 8:
        return False, False
    env = getattr(config, 'DASH_TOKEN', '')
    if env:
        return key == env, False
    h = hashlib.sha256(('teiki-dash:' + key).encode('utf-8')).hexdigest()
    owner = get_state(OWNER_KEY)
    if not owner:
        set_state(OWNER_KEY, {'h': h, 'at': config.now_jst().isoformat()})
        return True, True
    return (owner.get('h') if isinstance(owner, dict) else owner) == h, False


def _calendar(today_iso):
    from . import gcal
    if not gcal.is_enabled():
        return {'enabled': False, 'events': []}
    evs = []
    for e in gcal.get_events(today_iso):
        s, t = e.get('start_min', 0), e.get('end_min', 0)
        evs.append({'title': e.get('summary') or '(無題)', 'all_day': bool(e.get('all_day')),
                    'start': '' if e.get('all_day') else f'{s // 60:02d}:{s % 60:02d}',
                    'end': '' if e.get('all_day') else f'{t // 60:02d}:{t % 60:02d}'})
    return {'enabled': True, 'events': evs}


def _square(y, first):
    from . import square
    if not square.is_enabled():
        return None
    ys = square.summarize(y)
    ms = square.summarize(y, begin_date=first) if first <= y else {'total': 0}
    if ys is None or ms is None:
        raise RuntimeError('Squareから読めませんでした')
    return {'yesterday': ys['total'], 'count': ys['count'], 'month': ms['total']}


def _air_total(date_from, date_to):
    from . import airregi_web as a
    r = a.api('/api/searchReceipt/execute/', {'businessDayFrom': date_from.replace('-', ''),
                                              'businessDayTo': date_to.replace('-', ''), 'salesJournalSearchType': '0'})
    rows = (((r.json() or {}).get('results') or {}).get('resultsData') or {}).get('journal') or []
    paid = a._paid(rows)
    return sum(a._yen(j.get('moneyAmount')) for j in paid), len(paid)


def _air(y, first):
    from . import airregi_web
    if not airregi_web.is_enabled():
        return None
    yt, yc = _air_total(y, y)
    mt = _air_total(first, y)[0] if first <= y else 0
    return {'yesterday': yt, 'count': yc, 'month': mt}


def _usen(y, first):
    from . import usen
    if not usen.is_enabled():
        return None
    ys = [o for o in usen.parse_slips(usen._search(y, y)) if not o.get('date') or o['date'] == y]
    mt = sum(o['total'] for o in usen.parse_slips(usen._search(first, y))) if first <= y else 0
    return {'yesterday': sum(o['total'] for o in ys), 'count': len(ys), 'month': mt}


def _sales(today_iso, fresh=False):
    key = f'dash:sales:{today_iso}'
    cached = get_state(key)
    if not fresh and isinstance(cached, dict) and time.time() - cached.get('t', 0) < SALES_TTL_SEC:
        return cached
    d = date.fromisoformat(today_iso)
    y = (d - timedelta(days=1)).isoformat()
    first = d.replace(day=1).isoformat()
    stores = []
    # レジごとに別の店：Square=屋台うなぎ、Airレジ=ハンバーグ日和、USENレジ=鰻いち（name はレジの名前）
    for sid, name, fn in (('square', 'Square', _square), ('air', 'Airレジ', _air), ('usen', 'USENレジ', _usen)):
        try:
            r = fn(y, first)
            if r is None:
                continue
            stores.append({'id': sid, 'name': name, **r})
        except Exception as e:
            print('dash sales error', sid, e)
            stores.append({'id': sid, 'name': name, 'error': str(e)[:120]})
    out = {'t': time.time(), 'at': config.now_jst().strftime('%H:%M'), 'yesterday_date': y, 'stores': stores}
    set_state(key, out)
    return out


# 屋台うなぎの経費。勤怠アプリ・勤怠bot（birdmen_kv）の日ごとの収支（birdmen:report:日付）の出金を、
# 種類ごとに今月の1日から昨日まで足す。人件費＝勤怠のシフト合計、原材料・消耗品＝勤怠botで読んだレシート。
# 固定費（birdmen:fixed-costs）は支払日に関係なく今月分をまるごと入れる（月の途中で利益が大きく動かないように）。
COST_NAMES = {'labor': '人件費', 'ingredient': '原材料', 'supplies': '消耗品'}


def _costs(today_iso):
    d = date.fromisoformat(today_iso)
    month = today_iso[:7]
    rows = get_supabase('birdmen_kv', f'key=like.{quote("birdmen:report:" + month + "-")}*&select=key,value')
    by, days = {}, 0
    for r in rows:
        day = r['key'].rsplit(':', 1)[-1]
        if day >= today_iso or not isinstance(r.get('value'), dict):
            continue
        days += 1
        for acct in ('corp', 'pers', 'cash'):
            for w in ((r['value'].get(acct) or {}).get('withdraws') or []):
                try:
                    amt = float(w.get('amount') or 0)
                except (TypeError, ValueError):
                    continue
                cat = w.get('category') or 'other'
                by[cat] = by.get(cat, 0) + amt
    fixed_rows = get_supabase('birdmen_kv', 'key=eq.birdmen:fixed-costs&select=value')
    fixed_list = (fixed_rows[0].get('value') if fixed_rows else None) or []
    fixed = [{'label': f.get('label') or '', 'amount': int(float(f.get('amount') or 0)), 'day': f.get('day')}
             for f in fixed_list if isinstance(f, dict)]
    cats = [{'id': k, 'name': COST_NAMES.get(k, 'その他'), 'amount': int(v)} for k, v in sorted(by.items(), key=lambda x: -x[1])]
    return {'days': days, 'by': cats, 'labor': int(by.get('labor', 0)),
            'variable': int(sum(by.values())), 'fixed': sum(f['amount'] for f in fixed), 'fixed_items': fixed,
            'first_day': d.day == 1}


# ---------- 別会社の店の詳しい数字（ハンバーグ日和＝Airレジ、鰻いち＝USENレジ） ----------
# 客数・組数・客単価・出数・時間別の売上と人数、鰻いちは滞在時間も。注文の中身はレジの詳細を1件ずつ開くので
# 1回に最大30件ずつ読み、全部読めたら日付ごとに保存して使い回す。

def _hm(t):
    try:
        h, m = str(t).split(':')[:2]
        return int(h) * 60 + int(m)
    except (ValueError, AttributeError):
        return None


def _stats(orders, with_stay):
    dine = [o for o in orders if not (o.get('takeout') or o.get('bento'))]
    to = [o for o in orders if o.get('takeout') or o.get('bento')]
    sales = sum(o.get('total') or 0 for o in orders)
    dine_sales = sum(o.get('total') or 0 for o in dine)
    guests = sum(o.get('guests') or 0 for o in dine)
    hours = {}
    for o in orders:
        mm = _hm(o.get('in') or o.get('time')) if with_stay else _hm(o.get('time'))
        if mm is None:
            continue
        h = hours.setdefault(mm // 60, {'h': mm // 60, 'sales': 0, 'groups': 0, 'guests': 0})
        h['sales'] += o.get('total') or 0
        h['groups'] += 1
        if not (o.get('takeout') or o.get('bento')):
            h['guests'] += o.get('guests') or 0
    items = {}
    for o in orders:
        for it in o.get('items') or []:
            n = (it.get('name') or '').strip()
            if n:
                items[n] = items.get(n, 0) + int(it.get('qty') or 1)
    out = {'sales': sales, 'orders': len(orders), 'groups': len(dine), 'guests': guests,
           'per_guest': round(dine_sales / guests) if guests else None,
           'per_group': round(dine_sales / len(dine)) if dine else None,
           'takeout': {'count': len(to), 'sales': sum(o.get('total') or 0 for o in to)},
           'hourly': [hours[k] for k in sorted(hours)],
           'items': sorted(({'name': k, 'qty': v} for k, v in items.items()), key=lambda x: -x['qty'])[:20],
           'items_read': sum(1 for o in orders if o.get('items'))}
    if with_stay:
        mins = []
        for o in dine:
            a, b = _hm(o.get('in')), _hm(o.get('out'))
            if a is not None and b is not None:
                if b < a:
                    b += 24 * 60
                mins.append(b - a)
        if mins:
            mins.sort()
            buckets = [('〜30分', 0, 30), ('30〜60分', 30, 60), ('60〜90分', 60, 90), ('90〜120分', 90, 120), ('120分〜', 120, 10 ** 6)]
            out['stay'] = {'avg': round(sum(mins) / len(mins)), 'median': mins[len(mins) // 2], 'count': len(mins),
                           'buckets': [{'label': lb, 'count': sum(1 for x in mins if lo <= x < hi)} for lb, lo, hi in buckets]}
    return out


def store_detail(sid, day):
    key = f'dash:storedetail:{sid}:{day}'
    cached = get_state(key)
    if isinstance(cached, dict) and cached.get('complete'):
        return cached
    if sid == 'air':
        from . import airregi_web
        if not airregi_web.is_enabled():
            return {'ok': False, 'error': 'Airレジがbotにつながっていません'}
        r = airregi_web.day_sales(day)
        if not r.get('ok'):
            return {'ok': False, 'error': r.get('error') or 'Airレジを読めませんでした'}
        orders = r.get('orders') or []
        read = sum(1 for o in orders if o.get('cash') is not None or o.get('items'))
        out = {'ok': True, 'store': 'ハンバーグ日和', 'register': 'Airレジ', 'date': day, **_stats(orders, False)}
    elif sid == 'usen':
        from . import usen
        if not usen.is_enabled():
            return {'ok': False, 'error': 'USENレジがbotにつながっていません'}
        html = usen._search(day, day)
        orders = [o for o in usen.parse_slips(html) if not o.get('date') or o['date'] == day]
        try:
            usen._fill_items(day, html, orders)
        except Exception as e:  # 注文の中身が取れなくても、ほかの数字は出す
            print('dash usen items failed:', e)
        read = sum(1 for o in orders if o.get('items'))
        out = {'ok': True, 'store': '鰻いち', 'register': 'USENレジ', 'date': day, **_stats(orders, True)}
    else:
        return {'ok': False, 'error': 'その店はありません'}
    out['read'] = read
    out['complete'] = read >= out['orders']
    out['at'] = config.now_jst().strftime('%H:%M')
    if out['complete']:
        set_state(key, out)
    return out


def build(today_iso, fresh=False):
    out = {'ok': True, 'date': today_iso}
    try:
        out['calendar'] = _calendar(today_iso)
    except Exception as e:
        out['calendar'] = {'enabled': True, 'events': [], 'error': str(e)[:120]}
    out['sales'] = _sales(today_iso, fresh)
    try:
        out['costs'] = _costs(today_iso)
    except Exception as e:
        print('dash costs error', e)
        out['costs'] = {'error': str(e)[:120]}
    mail = get_state('dash:mail')
    out['mail'] = mail if isinstance(mail, dict) else None
    return out
