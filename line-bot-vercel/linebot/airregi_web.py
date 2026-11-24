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
_SESSION = {'s': None, 'at': 0, 'url': ''}


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
        _SESSION['at'], _SESSION['url'] = time.time(), r.url
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
    """管理画面が使っている /api/... を、画面と同じ形（JSONをPOST、XSRF-TOKENを見出しに）で呼ぶ。"""
    s = _session()
    if not s.cookies.get('XSRF-TOKEN', domain='airregi.jp') and not s.cookies.get('XSRF-TOKEN'):
        s.get('https://airregi.jp/CLP/view/salesJournal/', timeout=30)  # 取引履歴の画面を開いて、合言葉のクッキーをもらう
    token = s.cookies.get('XSRF-TOKEN') or ''
    r = s.post('https://airregi.jp/CLP' + path,
               json=payload, timeout=60,
               headers={'X-XSRF-TOKEN': token, 'Accept': 'application/json, text/plain, */*', 'Referer': 'https://airregi.jp/CLP/view/salesJournal/'})
    if urlparse(r.url).netloc == 'connect.airregi.jp':
        _SESSION['s'] = None
        raise RuntimeError('ログインが切れました（もう一度呼ぶとログインし直します）')
    return r


def _shape(v, depth=0):
    """JSONの「形」だけ（キーと型、配列の長さ）。値は数字を # に伏せ、文字は先頭だけ。"""
    if depth > 4:
        return '…'
    if isinstance(v, dict):
        return {k: _shape(x, depth + 1) for k, x in list(v.items())[:40]}
    if isinstance(v, list):
        return {'_len': len(v), '_first': _shape(v[0], depth + 1) if v else None}
    if isinstance(v, str):
        return _mask(v[:24])
    if isinstance(v, (int, float)):
        return '#'
    return v


def api_probe(path, payload):
    r = api(path, payload or {})
    out = {'ok': True, 'status': r.status_code, 'type': r.headers.get('content-type', '')}
    try:
        out['shape'] = _shape(r.json())
    except ValueError:
        out['text_masked'] = _mask(_text(r.text))[:400]
    return out
