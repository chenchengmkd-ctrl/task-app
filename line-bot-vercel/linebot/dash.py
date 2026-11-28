"""毎朝見るダッシュボード（dash.html）用。今日のGoogleカレンダーの予定、店ごとの売上（昨日・今月）、
朝のメールのまとめ（別の仕組みが dash:mail に置いたもの）を1回でまとめて返す。見るだけで、何も書き換えない。

- 売上はレジごとに取り方が違うので、1つ失敗してもほかは返す
- 売上は数十秒かかることがあるため、日付ごとに20分だけ使い回す
"""
import hashlib
import time
from datetime import date, timedelta

from . import config
from .supabase_client import get_state, set_state

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


def build(today_iso, fresh=False):
    out = {'ok': True, 'date': today_iso}
    try:
        out['calendar'] = _calendar(today_iso)
    except Exception as e:
        out['calendar'] = {'enabled': True, 'events': [], 'error': str(e)[:120]}
    out['sales'] = _sales(today_iso, fresh)
    mail = get_state('dash:mail')
    out['mail'] = mail if isinstance(mail, dict) else None
    return out
