"""Airレジ（管理画面 airregi.jp）から会計を取り出す（Airレジの店用）。

公式の「データ連携API」は審査中のため、オーナーの判断で、管理画面にログインして読み取る。
ログイン情報は Vercel の環境変数にだけ置く:
  AIRREGI_LOGIN_ID       … AirID（メールアドレスなど）
  AIRREGI_LOGIN_PASSWORD … パスワード

アカウントのロックを避けるため:
- ログインに失敗しても、自動ではやり直さない
- 画像認証（captcha）を求められたら、その場で止めて知らせる
- ログインしたセッションは10分使い回す

probe() は画面の「構造」だけを返す（リンク・フォーム項目・表の見出し・行数）。数字はすべて # に伏せる。
"""
import re
import time
from html import unescape
from urllib.parse import urljoin, urlparse

import requests

from . import config

START = 'https://airregi.jp/CLP/view/top/'
UA = 'Mozilla/5.0 (iPad; CPU OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1'
_SESSION = {'s': None, 'at': 0, 'url': '', 'app': None}


def is_enabled():
    return bool(config.AIRREGI_LOGIN_ID and config.AIRREGI_LOGIN_PASSWORD)


def _text(html):
    return re.sub(r'\s+', ' ', unescape(re.sub(r'<[^>]+>', ' ', html or ''))).strip()


def _mask(s):
    return re.sub(r'\d', '#', s or '')


def login():
    """AirIDでログインして、Airレジの管理画面に入ったセッションを返す。失敗したら RuntimeError（やり直さない）。"""
    s = requests.Session()
    s.headers.update({'User-Agent': UA, 'Accept-Language': 'ja-JP,ja;q=0.9'})
    r = s.get(START, timeout=30)
    if 'connect.airregi.jp' not in r.url:
        raise RuntimeError('ログイン画面に移りませんでした（Airレジの作りが変わったかもしれません）')
    form = re.search(r'<form[^>]*id="command"[^>]*action="([^"]*)"[^>]*>(.*?)</form>', r.text, re.S)
    if not form:
        raise RuntimeError('ログインの入力欄が見つかりません（Airレジの作りが変わったかもしれません）')
    data = {}
    for tag in re.findall(r'<input[^>]*>', form.group(2)):
        name = re.search(r'name="([^"]+)"', tag)
        typ = (re.search(r'type="([^"]+)"', tag) or [None, 'text'])[1].lower()
        if not name or typ in ('submit', 'button', 'checkbox'):
            continue
        data[name.group(1)] = unescape((re.search(r'value="([^"]*)"', tag) or [None, ''])[1])
    if data.get('captchaRequired', '0') != '0':
        raise RuntimeError('Airレジが画像認証を求めています。自動ログインはここで止めます（ブラウザで一度ログインしてから、しばらく待ってください）')
    for k in list(data):
        if k.startswith('dummy'):
            data[k] = ''  # 自動入力を見分けるための欄。人が入力するときと同じく空にする
    data['username'] = config.AIRREGI_LOGIN_ID
    data['password'] = config.AIRREGI_LOGIN_PASSWORD
    r = s.post(urljoin(r.url, unescape(form.group(1))), data=data, timeout=40)
    host = urlparse(r.url).netloc
    if host == 'connect.airregi.jp':
        if re.search(r'name="captchaRequired"\s+value="1"', r.text) or re.search(r'g-recaptcha|captcha-image|captchaImage', r.text):
            raise RuntimeError('Airレジが画像認証を求めています。自動ログインはここで止めます')
        err = re.search(r'class="[^"]*(?:error|alert|serverError)[^"]*"[^>]*>(.*?)</', r.text, re.S)
        msg = _text(err.group(1))[:100] if err else ''
        raise RuntimeError('ログインできませんでした' + (f'（{msg}）' if msg else '（AirIDかパスワードを確認してください。続けて失敗するとロックされることがあるので、自動ではやり直しません）'))
    return s, r


def _session():
    if _SESSION['s'] is None or time.time() - _SESSION['at'] > 600:
        _SESSION['s'], r = login()
        _SESSION['at'], _SESSION['url'], _SESSION['app'] = time.time(), r.url, None
    return _SESSION['s']


def probe(path=None, find=None):
    """ログインして、トップ（または path）の画面の構造を返す。売上・客の値は返さない（数字は # に伏せる）。"""
    s = _session()
    url = urljoin('https://airregi.jp/', path) if path else (_SESSION['url'] or START)
    r = s.get(url, timeout=30)
    if urlparse(r.url).netloc == 'connect.airregi.jp':
        _SESSION['s'] = None
        raise RuntimeError('ログインが切れました（もう一度呼ぶとログインし直します）')
    html = r.text
    links = []
    for href, label in re.findall(r'<a[^>]+href="([^"#][^"]*)"[^>]*>(.*?)</a>', html, re.S):
        t = _text(label)
        if t and not href.startswith('javascript'):
            links.append({'text': t[:40], 'href': urljoin(r.url, unescape(href))})
    keep = re.compile(r'売上|会計|取引|伝票|日報|精算|レポート|分析|履歴|CSV|csv|商品|メニュー', re.I)
    forms = [{'action': _mask(a), 'fields': re.findall(r'name="([^"]+)"', b)[:40]}
             for a, b in re.findall(r'<form[^>]*action="([^"]*)"[^>]*>(.*?)</form>', html, re.S)]
    tables = [{'headers': [_text(h)[:20] for h in re.findall(r'<th[^>]*>(.*?)</th>', t, re.S)][:20],
               'rows': len(re.findall(r'<tr', t))} for t in re.findall(r'<table.*?</table>', html, re.S)[:5]]
    near = []
    if find:
        for m in re.finditer(re.escape(find), html):
            near.append(_mask(html[max(0, m.start() - 300):m.end() + 300]))
            if len(near) >= 6:
                break
    return {
        'ok': True,
        'url': _mask(r.url),
        'status': r.status_code,
        'title': _text((re.search(r'<title>(.*?)</title>', html, re.S) or [None, ''])[1])[:60],
        'links': [l for l in links if keep.search(l['text']) or keep.search(l['href'])][:60],
        'all_link_count': len(links),
        'forms': forms[:5],
        'tables': tables,
        'scripts': sorted(set(re.findall(r'src="([^"]+\.js)[^"]*"', html)))[:25],
        'api_urls': sorted(set(_mask(u) for u in re.findall(r'''["'](/[A-Za-z0-9_\-/]*(?:api|json|ajax|data)[A-Za-z0-9_\-/]*)["']''', html, re.I)))[:40],
        'near': near,
    }


# ---------- 管理画面の内部API（画面が使っているもの） ----------
def api(path, payload):
    """管理画面が使っている /api/... を、画面と同じ形で呼ぶ。
    画面の作り：paramStr=（JSON文字列）をフォームでPOST。見出しに corClpKeyCd（画面の APP 設定の値）と
    Native-Application-Version を付ける（XSRF-TOKEN クッキーがあれば X-XSRF-TOKEN にも）。"""
    import json
    s = _session()
    if not _SESSION.get('app'):
        page = s.get('https://airregi.jp/CLP/view/salesJournal/', timeout=30)
        if urlparse(page.url).netloc == 'connect.airregi.jp':
            _SESSION['s'] = None
            raise RuntimeError('ログインが切れました（もう一度呼ぶとログインし直します）')
        app = {}
        for k in ('basepath', 'corClpKeyCd', 'nativeAplicationVersion'):
            m = re.search(k + r"""\s*[:=]\s*["']([^"']*)["']""", page.text)
            app[k] = m.group(1) if m else ''
        _SESSION['app'] = app
    app = _SESSION['app']
    headers = {'Accept': 'application/json, text/plain, */*', 'Referer': 'https://airregi.jp/CLP/view/salesJournal/',
               'corClpKeyCd': app.get('corClpKeyCd', ''), 'Native-Application-Version': app.get('nativeAplicationVersion', ''),
               'If-Unmodified-Since': time.strftime('%a, %d %b %Y %H:%M:%S GMT', time.gmtime())}
    token = s.cookies.get('XSRF-TOKEN')
    if token:
        headers['X-XSRF-TOKEN'] = token
    base = (app.get('basepath') or '/CLP').rstrip('/')
    if not base.startswith('http'):  # 画面の設定は「https://airregi.jp/CLP」のように丸ごと入っていることがある
        base = 'https://airregi.jp' + base
    r = s.post(base + path, data={'paramStr': json.dumps(payload or {}, ensure_ascii=False)},
               headers=headers, timeout=60)
    if urlparse(r.url).netloc == 'connect.airregi.jp':
        _SESSION['s'] = None
        _SESSION['app'] = None
        raise RuntimeError('ログインが切れました（もう一度呼ぶとログインし直します）')
    return r


def _shape(v, depth=0, limit=9):
    """JSONの「形」だけ（キーと型、配列の長さ）。値は数字を # に伏せ、文字は先頭だけ。"""
    if depth > limit:
        return '…'
    if isinstance(v, dict):
        return {k: _shape(x, depth + 1, limit) for k, x in list(v.items())[:60]}
    if isinstance(v, list):
        return {'_len': len(v), '_first': _shape(v[0], depth + 1, limit) if v else None}
    if isinstance(v, str):
        return _mask(v[:24])
    if isinstance(v, (int, float)):
        return '#'
    return v


def api_probe(path, payload):
    r = api(path, payload or {})
    app = _SESSION.get('app') or {}
    out = {'ok': True, 'status': r.status_code, 'type': r.headers.get('content-type', ''),
           'app_found': {k: bool(v) for k, v in app.items()}, 'basepath': app.get('basepath')}
    try:
        out['shape'] = _shape(r.json())
    except ValueError:
        out['text_masked'] = _mask(_text(r.text))[:400]
    return out


def _journal(date_iso):
    """その営業日の取引履歴（一覧）。"""
    ymd = date_iso.replace('-', '')
    r = api('/api/searchReceipt/execute/', {'businessDayFrom': ymd, 'businessDayTo': ymd, 'salesJournalSearchType': '0'})
    res = (r.json() or {}).get('results') or {}
    if res.get('resultsData') is None:
        raise RuntimeError('取引履歴を読めませんでした（' + str(res.get('errMsg') or res.get('returnCode')) + '）')
    return res['resultsData'].get('journal') or []


def probe_receipt(date_iso):
    """その日の最初の会計（C/S）のレシート内容の「形」を返す（数字は # に伏せる）。"""
    rows = _journal(date_iso)
    types = {}
    for j in rows:
        types[j.get('journalRecordType')] = types.get(j.get('journalRecordType'), 0) + 1
    first = next((j for j in rows if j.get('journalRecordType') in ('C', 'S', 'CV', 'SV')), None)
    out = {'ok': True, 'types': types}
    if first:
        r = api('/api/receiptData/getText/', {'visStoreId': first['visStoreId'], 'journalRecordType': first['journalRecordType']})
        try:
            j = r.json()
            out['receipt_shape'] = _shape(j)
            h = (((j.get('results') or {}).get('resultsData') or {}).get('receiptHtmlElement')) or ''
            out['receipt_rows_masked'] = [[_mask(_text(c))[:30] for c in re.findall(r'<t[dh][^>]*>(.*?)</t[dh]>', tr, re.S)]
                                          for tr in re.findall(r'<tr[^>]*>(.*?)</tr>', h, re.S)][:40]
        except ValueError:
            out['receipt_text_masked'] = _mask(_text(r.text))[:600]
    return out


# ---------- 日報アプリ向け：その日の会計の一覧 ----------
# 売上に数える取引：会計(C)・分割会計(S)・修正した会計(CV/SV)。
# 返品(RT)・返品された会計(CR/SR/CVR/SVR)・レジ操作(RC/RR/RP/RF/RE)・未会計(UC)は数えない
PAID_TYPES = ('C', 'S', 'CV', 'SV')
_SKIP_ROWS = re.compile(r'^(小計|合計|\(|（|お預り|お預かり|おつり|お釣り|クレジット|現金|電子マネー|QR|ポイント|値引|割引)')


def _receipt_cash(html):
    """レシートのうち、現金で払った金額。「現金」の行があればその金額。支払い方法の行が無ければ全額現金。"""
    rows = [[_text(c) for c in re.findall(r'<t[dh][^>]*>(.*?)</t[dh]>', tr, re.S)] for tr in re.findall(r'<tr[^>]*>(.*?)</tr>', html or '', re.S)]
    total, after, methods = 0, False, []
    for c in rows:
        if not c or not c[0]:
            continue
        if c[0].startswith('合計'):
            total, after = _yen(re.sub(r'[^\d]', '', c[-1])), True
            continue
        if after and not re.match(r'^(お預り|お預かり|おつり|お釣り|\(|（|内消費税)', c[0]):
            methods.append((c[0], _yen(re.sub(r'[^\d]', '', c[-1]))))
    cash = [v for k, v in methods if '現金' in k]
    if cash:
        return sum(cash)
    return 0 if methods else total


def _receipt_items(html):
    """レシートの表から [{name, qty}] を作る。品目の下の「ソース/…」などの1マスの行はオプションなので数えない。"""
    items = []
    for tr in re.findall(r'<tr[^>]*>(.*?)</tr>', html or '', re.S):
        cells = [_text(c) for c in re.findall(r'<t[dh][^>]*>(.*?)</t[dh]>', tr, re.S)]
        if len(cells) < 3 or not cells[0] or _SKIP_ROWS.search(cells[0]):
            continue
        q = re.sub(r'[^\d]', '', cells[2]) if len(cells) >= 4 else ''
        if q.isdigit():
            items.append({'name': cells[0], 'qty': int(q)})
    return items


def _paid(rows):
    return [j for j in rows if j.get('journalRecordType') in PAID_TYPES and str(j.get('delFlg', '0')) in ('0', '', 'None')]


def _yen(v):
    try:
        return int(float(str(v).replace(',', '')))
    except (TypeError, ValueError):
        return 0


def day_sales(date_iso):
    """日報アプリ（nippo-air.html）用。Squareの day_sales と同じ形で返す。"""
    from .supabase_client import get_state, set_state
    if not is_enabled():
        return {'ok': False, 'error': 'Airレジのログイン情報が未設定です'}
    rows = _paid(_journal(date_iso))
    key = f'airregi:receipts2:{date_iso}'  # 2: 現金の金額も入れるようにしたもの
    cache = get_state(key) or {}
    missing = [j for j in rows if j['visStoreId'] not in cache][:30]  # 1回で開くのは最大30件（残りは次に押したときに取る）
    for j in missing:
        try:
            r = api('/api/receiptData/getText/', {'visStoreId': j['visStoreId'], 'journalRecordType': j['journalRecordType']})
            d = ((r.json() or {}).get('results') or {}).get('resultsData') or {}
        except (requests.RequestException, ValueError):
            break
        h = d.get('receiptHtmlElement')
        cache[j['visStoreId']] = {'items': _receipt_items(h), 'guests': _yen(d.get('visitorNum')), 'cash': _receipt_cash(h)}
    if missing:
        set_state(key, cache)
    orders = []
    for j in rows:
        c = cache.get(j['visStoreId']) or {}
        t = re.search(r'(\d{1,2}):(\d{2})', str(j.get('visStoreDate') or ''))
        orders.append({'id': 'air-' + j['visStoreId'], 'time': f'{int(t.group(1)):02d}:{t.group(2)}' if t else '',
                       'total': _yen(j.get('moneyAmount')), 'items': c.get('items', []), 'guests': c.get('guests', 0),
                       'cash': c.get('cash'),
                       # お弁当・テイクアウトの会計（店内の組数・客数と分けて数える）
                       'bento': any(re.search(r'弁当|テイクアウト|持ち帰り', i.get('name', '')) for i in c.get('items', [])),
                       'table': '', 'takeout': False})
    orders.sort(key=lambda o: o['time'])
    total = sum(o['total'] for o in orders)
    cash = sum(o['cash'] or 0 for o in orders) if all(o['cash'] is not None for o in orders) else None  # 中身をまだ読めていない会計があれば出さない
    return {'ok': True, 'date': date_iso, 'total': total, 'cash': cash, 'count': len(orders), 'orders': orders,
            'month_total': _month_before(date_iso) + total}


def _month_before(date_iso):
    """その月の1日から前日までの売上。日中は変わらないので、日付ごとに保存して使い回す。"""
    from datetime import date, timedelta
    from .supabase_client import get_state, set_state
    d = date.fromisoformat(date_iso)
    if d.day == 1:
        return 0
    key = f'airregi:month_before:{date_iso}'
    cached = get_state(key)
    if isinstance(cached, dict) and 'total' in cached:
        return int(cached['total'])
    first, prev = d.replace(day=1).strftime('%Y%m%d'), (d - timedelta(days=1)).strftime('%Y%m%d')
    r = api('/api/searchReceipt/execute/', {'businessDayFrom': first, 'businessDayTo': prev, 'salesJournalSearchType': '0'})
    rows = (((r.json() or {}).get('results') or {}).get('resultsData') or {}).get('journal') or []
    total = sum(_yen(j.get('moneyAmount')) for j in _paid(rows))
    set_state(key, {'total': total})
    return total


def menu_names():
    """Airレジに登録してあるメニューの名前（日報アプリのボタンを決めるため）。売上の値は含まない。"""
    r = api('/api/menuList/read/', {})
    rows = ((((r.json() or {}).get('results') or {}).get('resultsData') or {}).get('menuList')) or []
    return {'ok': True, 'menus': [{'name': m.get('menuName'), 'category': m.get('categoryId'), 'stopped': m.get('tempStopFlg') == '1',
                                   'order': m.get('menuDispNo')} for m in rows]}


def probe_cash(date_iso):
    """現金の数え方の確認用：会計ごとに、レシートの「合計」より後の行の名前（支払い方法など）を返す。金額は返さない。"""
    out = []
    for j in _paid(_journal(date_iso))[:30]:
        r = api('/api/receiptData/getText/', {'visStoreId': j['visStoreId'], 'journalRecordType': j['journalRecordType']})
        h = (((r.json() or {}).get('results') or {}).get('resultsData') or {}).get('receiptHtmlElement') or ''
        rows = [[_text(c) for c in re.findall(r'<t[dh][^>]*>(.*?)</t[dh]>', tr, re.S)] for tr in re.findall(r'<tr[^>]*>(.*?)</tr>', h, re.S)]
        labels, after = [], False
        for c in rows:
            if c and c[0].startswith('合計'):
                after = True
                continue
            if after and c and c[0]:
                labels.append(_mask(c[0])[:24])
        out.append({'type': j['journalRecordType'], 'counted_as_cash': _receipt_cash(h) > 0, 'labels_after_total': labels,
                    'has_genkin_anywhere': '現金' in _text(h)})
    return {'ok': True, 'receipts': out}
