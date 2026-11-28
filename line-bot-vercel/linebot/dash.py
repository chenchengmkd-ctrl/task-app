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


# ---------- 店の数字（日・週・月）：屋台うなぎ＝Square（勤怠アプリの日ごとの集計）、ハンバーグ日和＝Airレジ、鰻いち＝USENレジ ----------
# 売上・組数はレジに期間まとめて1回問い合わせて出す。注文の中身（出数・Airレジの人数）はレジを1件ずつ開くので、
# 読んだ分だけ使い、残りは5分おきの見回り（backfill_step）と「続きを読む」（fill）で少しずつ貯めていく。
import re as _re

BACKFILL_DAYS = 45


def _hm(t):
    try:
        h, m = str(t).split(':')[:2]
        return int(h) * 60 + int(m)
    except (ValueError, AttributeError):
        return None


def _days(start, end):
    d0, d1 = date.fromisoformat(start), date.fromisoformat(end)
    return [(d0 + timedelta(days=i)).isoformat() for i in range((d1 - d0).days + 1)]


def _get_many(table, keys):
    """キーの一覧をまとめて読む → {key: value}"""
    out = {}
    for i in range(0, len(keys), 40):
        part = keys[i:i + 40]
        inlist = ','.join('"' + k.replace('"', '') + '"' for k in part)
        for r in get_supabase(table, f'key=in.({quote(inlist)})&select=key,value'):
            out[r['key']] = r.get('value')
    return out


def _stats(orders, with_stay):
    """会計の一覧から、売上・組数・客数・単価・時間別・出数・滞在時間・日ごとの売上をまとめる。"""
    dine = [o for o in orders if not (o.get('takeout') or o.get('bento'))]
    to = [o for o in orders if o.get('takeout') or o.get('bento')]
    known = [o for o in dine if o.get('guests')]  # 人数がわかっている会計（Airレジは中身を読んだ分だけ）
    sales = sum(o.get('total') or 0 for o in orders)
    guests = sum(o.get('guests') or 0 for o in known)
    known_sales = sum(o.get('total') or 0 for o in known)
    dine_sales = sum(o.get('total') or 0 for o in dine)
    hours, daily, items = {}, {}, {}
    for o in orders:
        mm = _hm((o.get('in') or o.get('time')) if with_stay else o.get('time'))
        tko = o.get('takeout') or o.get('bento')
        if mm is not None:
            h = hours.setdefault(mm // 60, {'h': mm // 60, 'sales': 0, 'groups': 0, 'guests': 0})
            h['sales'] += o.get('total') or 0
            h['groups'] += 0 if tko else 1
            h['guests'] += 0 if tko else (o.get('guests') or 0)
        if o.get('date'):
            dd = daily.setdefault(o['date'], {'date': o['date'], 'sales': 0, 'groups': 0, 'guests': 0})
            dd['sales'] += o.get('total') or 0
            dd['groups'] += 0 if tko else 1
            dd['guests'] += 0 if tko else (o.get('guests') or 0)
        for it in o.get('items') or []:
            n = (it.get('name') or '').strip()
            if n:
                items.setdefault(n, {'name': n, 'qty': 0})['qty'] += int(it.get('qty') or 1)
    out = {'sales': sales, 'orders': len(orders), 'groups': len(dine), 'guests': guests,
           'guests_known_groups': len(known),
           'per_guest': round(known_sales / guests) if guests else None,
           'per_group': round(dine_sales / len(dine)) if dine else None,
           'takeout': {'count': len(to), 'sales': sum(o.get('total') or 0 for o in to)},
           'hourly': [hours[k] for k in sorted(hours)],
           'daily': [daily[k] for k in sorted(daily)],
           'items': sorted(items.values(), key=lambda x: -x['qty'])[:20],
           'read': sum(1 for o in orders if o.get('items') or o.get('cash') is not None)}
    if with_stay:
        mins = []
        for o in dine:
            a, b = _hm(o.get('in')), _hm(o.get('out'))
            if a is not None and b is not None:
                mins.append(b - a + (24 * 60 if b < a else 0))
        if mins:
            mins.sort()
            buckets = [('〜30分', 0, 30), ('30〜60分', 30, 60), ('60〜90分', 60, 90), ('90〜120分', 90, 120), ('120分〜', 120, 10 ** 6)]
            out['stay'] = {'avg': round(sum(mins) / len(mins)), 'median': mins[len(mins) // 2], 'count': len(mins),
                           'buckets': [{'label': lb, 'count': sum(1 for x in mins if lo <= x < hi)} for lb, lo, hi in buckets]}
    return out


def _square_range(days):
    """屋台うなぎ：勤怠アプリが保存している日ごとの集計（birdmen:sales:日付）を足す。組数はないので客数のみ。"""
    got = _get_many('birdmen_kv', ['birdmen:sales:' + d for d in days])
    sales = guests = 0
    hours, items, daily, usage = {}, {}, [], {}
    for d in days:
        v = got.get('birdmen:sales:' + d)
        if not isinstance(v, dict):
            continue
        t, c = int(v.get('total') or 0), int(v.get('customers') or 0)
        sales += t
        guests += c
        daily.append({'date': d, 'sales': t, 'guests': c, 'groups': None})
        for h, n in (v.get('byHour') or {}).items():
            try:
                hh = int(h)
            except ValueError:
                continue
            hours.setdefault(hh, {'h': hh, 'sales': None, 'groups': None, 'guests': 0})['guests'] += int(n or 0)
        for it in v.get('items') or []:
            n = (it.get('name') or '').strip()
            if n:
                x = items.setdefault(n, {'name': n, 'qty': 0, 'amount': 0})
                x['qty'] += int(it.get('qty') or 0)
                x['amount'] += int(it.get('amount') or 0)
        for k, val in (v.get('usage') or {}).items():
            try:
                usage[k] = usage.get(k, 0) + float(val or 0)
            except (TypeError, ValueError):
                pass
    return {'store': '屋台うなぎ', 'register': 'Square', 'sales': sales, 'orders': None, 'groups': None, 'guests': guests,
            'per_guest': round(sales / guests) if guests else None, 'per_group': None, 'takeout': None,
            'hourly': [hours[k] for k in sorted(hours)], 'daily': daily,
            'items': sorted(items.values(), key=lambda x: -x['qty'])[:20],
            'usage': {k: round(v, 1) for k, v in usage.items()}, 'days_with_data': len(daily), 'complete': True}


def _usen_orders(start, end):
    from . import usen
    html = usen._search(start, end)
    orders = [o for o in usen.parse_slips(html) if not o.get('date') or start <= o['date'] <= end]
    for o in orders:
        if not o.get('date') and start == end:
            o['date'] = start
    caches = _get_many('bot_state', ['usen:items:' + d for d in _days(start, end)])
    for o in orders:
        o['items'] = (caches.get('usen:items:' + (o.get('date') or '')) or {}).get(o['id'], [])
        o.pop('args', None)
    return orders


def _air_date(j, fallback):
    for k in ('businessDay', 'businessDate', 'salesDate', 'visStoreDate', 'accountingDate'):
        m = _re.search(r'(\d{4})[/-]?(\d{2})[/-]?(\d{2})', str(j.get(k) or ''))
        if m:
            return f'{m.group(1)}-{m.group(2)}-{m.group(3)}'
    return fallback


def _air_orders(start, end):
    from . import airregi_web as a
    r = a.api('/api/searchReceipt/execute/', {'businessDayFrom': start.replace('-', ''), 'businessDayTo': end.replace('-', ''),
                                              'salesJournalSearchType': '0'})
    rows = a._paid((((r.json() or {}).get('results') or {}).get('resultsData') or {}).get('journal') or [])
    caches = _get_many('bot_state', ['airregi:receipts3:' + d for d in _days(start, end)])
    allc = {}
    for v in caches.values():
        if isinstance(v, dict):
            allc.update(v)
    orders = []
    for j in rows:
        c = allc.get(j['visStoreId']) or {}
        t = _re.search(r'(\d{1,2}):(\d{2})', str(j.get('visStoreDate') or ''))
        items = c.get('items', [])
        orders.append({'id': 'air-' + j['visStoreId'], 'date': _air_date(j, start if start == end else ''),
                       'time': f'{int(t.group(1)):02d}:{t.group(2)}' if t else '', 'total': a._yen(j.get('moneyAmount')),
                       'guests': c.get('guests', 0), 'items': items, 'cash': c.get('cash'),
                       'bento': any(_re.search(r'弁当|テイクアウト|持ち帰り', i.get('name', '')) for i in items)})
    return orders


def store_range(sid, start, end):
    """その店の、start〜end（両端を含む）の数字。"""
    if sid == 'square':
        return {'ok': True, 'start': start, 'end': end, **_square_range(_days(start, end))}
    if sid == 'usen':
        from . import usen
        if not usen.is_enabled():
            return {'ok': False, 'error': 'USENレジがbotにつながっていません'}
        orders = _usen_orders(start, end)
        out = {'store': '鰻いち', 'register': 'USENレジ', **_stats(orders, True)}
        out['read'] = sum(1 for o in orders if o.get('items'))
    elif sid == 'air':
        from . import airregi_web
        if not airregi_web.is_enabled():
            return {'ok': False, 'error': 'Airレジがbotにつながっていません'}
        orders = _air_orders(start, end)
        out = {'store': 'ハンバーグ日和', 'register': 'Airレジ', **_stats(orders, False)}
        out['read'] = sum(1 for o in orders if o.get('cash') is not None or o.get('items'))
    else:
        return {'ok': False, 'error': 'その店はありません'}
    out['complete'] = out['read'] >= out['orders']
    return {'ok': True, 'start': start, 'end': end, **out}


def _fill_day(sid, day):
    """その日の注文の中身を最大30件読む。→ 全部読めたら True"""
    if sid == 'air':
        from . import airregi_web
        r = airregi_web.day_sales(day)
        return bool(r.get('ok')) and all(o.get('cash') is not None for o in r.get('orders') or [])
    from . import usen
    html = usen._search(day, day)
    orders = [o for o in usen.parse_slips(html) if not o.get('date') or o['date'] == day]
    usen._fill_items(day, html, orders)
    return all(o.get('items') for o in orders)


def fill(sid, start, end, budget_sec=35):
    """「続きを読む」：新しい日から順に、読み残しのある日の注文の中身を読む（時間の上限まで）。"""
    if sid not in ('air', 'usen'):
        return {'ok': True, 'done': True}
    t0, done = time.time(), True
    for d in reversed(_days(start, end)):
        if time.time() - t0 > budget_sec:
            done = False
            break
        try:
            if not _fill_day(sid, d):
                done = False
                break
        except Exception as e:
            return {'ok': False, 'error': str(e)[:150]}
    return {'ok': True, 'done': done}


def backfill_step(push_text_fn=None, get_users_fn=None):
    """5分おきの見回りから呼ばれる。昨日から順にさかのぼり、注文の中身を読み残している日を30件ずつ読む（店ごとに1日分）。"""
    today = date.fromisoformat(config.today_iso())
    for sid in ('usen', 'air'):
        try:
            if sid == 'usen':
                from . import usen as m
            else:
                from . import airregi_web as m
            if not m.is_enabled():
                continue
            key = f'dash:backfill:{sid}'
            st = get_state(key) or {}
            if st.get('today') != today.isoformat():  # 日が変わったら昨日からやり直す
                st = {'today': today.isoformat(), 'cursor': (today - timedelta(days=1)).isoformat()}
            cur = date.fromisoformat(st['cursor'])
            if (today - cur).days > BACKFILL_DAYS:
                continue
            if _fill_day(sid, cur.isoformat()):
                st['cursor'] = (cur - timedelta(days=1)).isoformat()
            set_state(key, st)
        except Exception as e:
            print('dash backfill error', sid, e)


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
