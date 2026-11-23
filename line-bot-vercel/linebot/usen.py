"""USENレジ（管理画面 u06.u-regi.com）から伝票を取り出す（2店舗目以降のUSENレジの店用）。

USENレジには外部向けのAPIがないため、オーナーの判断で、管理画面にログインして
「売上管理」→「伝票明細」を読み取る。ログイン情報は Vercel の環境変数にだけ置く:
  USEN_COMPANY_CODE … 企業コード
  USEN_USER_ID      … 担当者コード（この連携専用の担当者を作るのがおすすめ）
  USEN_PASSWORD     … パスワード
管理画面の作りが変わると読めなくなる。そのときは probe() で画面の構造を調べ直す。

probe() は画面の「構造」だけを返す（メニューのリンク、フォームの項目名、表の見出し、行数）。
売上や客の情報そのものは返さない。
"""
import re
from html import unescape
from urllib.parse import urljoin

import requests

from . import config

BASE = 'https://u06.u-regi.com/'
UA = 'Mozilla/5.0 (iPad; CPU OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1'


def is_enabled():
    return bool(config.USEN_COMPANY_CODE and config.USEN_USER_ID and config.USEN_PASSWORD)


def _text(html):
    return re.sub(r'\s+', ' ', unescape(re.sub(r'<[^>]+>', ' ', html or ''))).strip()


def login():
    """ログインしたセッションを返す。失敗したら RuntimeError。"""
    s = requests.Session()
    s.headers['User-Agent'] = UA
    r = s.get(BASE, timeout=20)
    m = re.search(r'name="csrf_token_name" value="([^"]*)"', r.text)
    if not m:
        raise RuntimeError('ログイン画面の形が変わっています（csrf_token_name が見つかりません）')
    r = s.post(urljoin(BASE, '/login/C_login/terminal_login'), timeout=20, data={
        'li_token': '', 'csrf_token_name': m.group(1),
        'company_code': config.USEN_COMPANY_CODE, 'user_id': config.USEN_USER_ID, 'password': config.USEN_PASSWORD,
    })
    if 'name="company_code"' in r.text and 'ログアウト' not in r.text:
        err = re.search(r'class="[^"]*(?:error|alert)[^"]*"[^>]*>(.*?)</', r.text, re.S)
        raise RuntimeError('ログインできませんでした' + (f'（{_text(err.group(1))[:80]}）' if err else '（企業コード・担当者コード・パスワードを確認してください）'))
    return s, r


def _links(html, base):
    out = []
    for href, label in re.findall(r'<a[^>]+href="([^"#][^"]*)"[^>]*>(.*?)</a>', html, re.S):
        t = _text(label)
        if t and not href.startswith('javascript'):
            out.append({'text': t[:40], 'href': urljoin(base, unescape(href))})
    return out


def _forms(html, base):
    forms = []
    for attrs, body in re.findall(r'<form([^>]*)>(.*?)</form>', html, re.S):
        action = re.search(r'action="([^"]*)"', attrs)
        method = re.search(r'method="([^"]*)"', attrs, re.I)
        fields = []
        for tag in re.findall(r'<(?:input|select|textarea)[^>]*>', body):
            name = re.search(r'name="([^"]+)"', tag)
            typ = re.search(r'type="([^"]+)"', tag)
            if name:
                fields.append({'name': name.group(1), 'type': (typ.group(1) if typ else tag[1:7]).lower()})
        forms.append({'action': urljoin(base, unescape(action.group(1))) if action else '', 'method': (method.group(1) if method else 'get').lower(),
                      'fields': fields[:60]})
    return forms


def _tables(html):
    out = []
    for t in re.findall(r'<table.*?</table>', html, re.S)[:5]:
        heads = [_text(h)[:30] for h in re.findall(r'<th[^>]*>(.*?)</th>', t, re.S)]
        rows = len(re.findall(r'<tr', t))
        out.append({'headers': heads[:20], 'rows': rows})
    return out


def probe(path=None, find=None, method='get', data=None):
    """ログインして、トップ（または path）の画面の構造を返す。売上・客の値は返さない。
    find を渡すと、その言葉の前後のHTML（画面の作り）を返す。数字はすべて # に伏せる。"""
    s, r = login()
    url = urljoin(BASE, path) if path else r.url
    if path:
        r = s.post(url, data=data or {}, timeout=30) if method == 'post' else s.get(url, timeout=30)
    html = r.text
    near = []
    if find:
        for m in re.finditer(re.escape(find), html):
            near.append(re.sub(r'\d', '#', html[max(0, m.start() - 300):m.end() + 300]))
            if len(near) >= 8:
                break
    links = _links(html, r.url)
    keep = re.compile(r'売上|伝票|帳票|汎用|検索|精算|日報|実績|CSV|csv', re.I)
    return {
        'ok': True,
        'url': r.url,
        'title': _text((re.search(r'<title>(.*?)</title>', html, re.S) or [None, ''])[1])[:60],
        'links': [l for l in links if keep.search(l['text']) or keep.search(l['href'])][:60],
        'all_link_count': len(links),
        'forms': _forms(html, r.url),
        'tables': _tables(html),
        'scripts': sorted(set(re.findall(r'src="([^"]+\.js)[^"]*"', html)))[:20],
        'ajax_urls': sorted(set(re.findall(r'''["'](/[A-Za-z0-9_/]+/C_[A-Za-z0-9_/]+)["']''', html)))[:60],
        'status': r.status_code,
        'near': near,
    }
