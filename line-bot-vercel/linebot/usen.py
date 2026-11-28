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


# ---------- 伝票明細の検索 ----------
SLIP_LIST = '/slip_detail/C_slip_detail/store_table_list'


def _form_defaults(html):
    """伝票明細の検索フォームの、今入っている値（hidden・チェック済み・選択中）を集める。"""
    m = re.search(r'<form[^>]*>(.*?)</form>', html, re.S)
    body = m.group(1) if m else html
    data = []
    for tag in re.findall(r'<input[^>]*>', body):
        name = re.search(r'name="([^"]+)"', tag)
        if not name:
            continue
        typ = (re.search(r'type="([^"]+)"', tag) or [None, 'text'])[1].lower()
        val = unescape((re.search(r'value="([^"]*)"', tag) or [None, ''])[1])
        if typ in ('button', 'submit'):
            continue
        if typ in ('checkbox', 'radio') and 'checked' not in tag:
            continue
        data.append((name.group(1), val))
    for name, inner in re.findall(r'<select[^>]*name="([^"]+)"[^>]*>(.*?)</select>', body, re.S):
        opts = re.findall(r'<option([^>]*)value="([^"]*)"', inner)
        sel = next((v for a, v in opts if 'selected' in a), opts[0][1] if opts else '')
        data.append((name, unescape(sel)))
    return data


def search_slips(date_iso):
    """その日の伝票明細の検索結果ページ（HTML）を返す。"""
    s, _ = login()
    page = s.get(urljoin(BASE, SLIP_LIST), timeout=30).text
    ymd = date_iso.replace('-', '')
    data = [(k, v) for k, v in _form_defaults(page) if k not in ('mode', 'search_sum_target_date_from', 'search_sum_target_date_to')]
    data += [('mode', 'search'), ('search_sum_target_date_from', ymd), ('search_sum_target_date_to', ymd)]
    r = s.post(urljoin(BASE, SLIP_LIST), data=data, timeout=40)
    return r.text, data


def probe_search(date_iso, find=None):
    """検索結果の「形」だけを返す（見出し・行数・1行目の数字を伏せたもの）。find の前後のHTMLも、数字を伏せて返す。"""
    html, sent = search_slips(date_iso)
    near = [re.sub(r'\d', '#', html[max(0, m.start() - 400):m.end() + 400]) for m in re.finditer(re.escape(find), html)][:4] if find else []
    t = re.search(r'<table[^>]*id="slip-list"[^>]*>(.*?)</table>', html, re.S) or re.search(r'<table[^>]*>(.*?)</table>', html, re.S)
    body = t.group(1) if t else ''
    heads = [_text(h)[:20] for h in re.findall(r'<th[^>]*>(.*?)</th>', body, re.S)]
    rows = re.findall(r'<tr[^>]*>(.*?)</tr>', body, re.S)
    data_rows = [r for r in rows if '<td' in r]
    first = [re.sub(r'\d', '#', _text(c))[:60] for c in re.findall(r'<td[^>]*>(.*?)</td>', data_rows[0], re.S)] if data_rows else []
    return {'ok': True, 'sent_fields': [k for k, _ in sent], 'headers': heads, 'rows': len(data_rows), 'first_row_masked': first,
            'has_table': bool(t), 'empty_msg': '検索結果はありませんでした' in html, 'near': near,
            'rows_shape': [{'c0': re.sub(r'\d', '#', _text(cs[0])) if cs else '', 'c3': _text(cs[3]) if len(cs) > 3 else '',
                            'branch': (_DETAIL_ARGS.search(r).group(3) if _DETAIL_ARGS.search(r) else None),
                            'c0_html': re.sub(r'\d', '#', re.sub(r'\s+', ' ', re.findall(r'<td[^>]*>(.*?)</td>', r, re.S)[0]))[:200] if cs else ''}
                           for r in data_rows for cs in [[c for c in re.findall(r'<td[^>]*>(.*?)</td>', r, re.S)]]],
            'ajax_urls': sorted(set(re.findall(r'''["'](/?[A-Za-z0-9_]+/C_[A-Za-z0-9_]+/[A-Za-z0-9_]+)["']''', html)))[:60]}


# ---------- 日報アプリ向け：その日の会計の一覧 ----------
# 続けて呼ばれたときのために、ログインしたセッションを少しの間使い回す（毎回ログインしない）
_SESSION = {'s': None, 'at': 0}


def _session():
    import time
    if _SESSION['s'] is None or time.time() - _SESSION['at'] > 600:
        _SESSION['s'], _ = login()
        _SESSION['at'] = time.time()
    return _SESSION['s']


def _search(date_from, date_to):
    """伝票明細を期間で検索して、結果のHTMLを返す。セッションが切れていたらログインし直す。"""
    for attempt in range(2):
        s = _session()
        page = s.get(urljoin(BASE, SLIP_LIST), timeout=30).text
        if 'name="company_code"' in page:  # ログイン画面に戻された
            _SESSION['s'] = None
            continue
        data = [(k, v) for k, v in _form_defaults(page) if k not in ('mode', 'search_sum_target_date_from', 'search_sum_target_date_to')]
        data += [('mode', 'search'), ('search_sum_target_date_from', date_from.replace('-', '')),
                 ('search_sum_target_date_to', date_to.replace('-', ''))]
        html = s.post(urljoin(BASE, SLIP_LIST), data=data, timeout=60).text
        if 'name="company_code"' in html:
            _SESSION['s'] = None
            continue
        return html
    raise RuntimeError('USENレジにログインできませんでした')


def _yen(text):
    m = re.search(r'金額\s*:?\s*[￥¥]?\s*([\d,]+)', text)
    return int(m.group(1).replace(',', '')) if m else 0


def parse_slips(html):
    """検索結果の表から、会計済みの伝票を [{id,time,total,table,guests,takeout,date,items}] にする。"""
    t = re.search(r'<table[^>]*id="slip-list"[^>]*>(.*?)</table>', html, re.S) or re.search(r'<table[^>]*>(.*?)</table>', html, re.S)
    out = []
    for row in re.findall(r'<tr[^>]*>(.*?)</tr>', t.group(1) if t else '', re.S):
        cells = [_text(c) for c in re.findall(r'<td[^>]*>(.*?)</td>', row, re.S)]
        if len(cells) < 7:
            continue
        joined = ' '.join(cells)
        # 数えるのは「会計済」と「黒伝票」。赤黒訂正（会計のやり直し）では、元の伝票が「支払取消」、
        # 打ち消しが「赤伝票」、正しい会計が「黒伝票」になる。USENの売上実績と同じく、黒伝票を売上に入れる
        status = cells[3]
        if not re.search(r'会計済|黒伝票', status) or re.search(r'支払取消|赤伝票', status):
            continue
        slip = re.sub(r'\s+', '', re.sub(r'[(（].*', '', cells[0]))  # 黒伝票は「番号 (元伝票の番号)」なので、括弧の前だけ
        args = _DETAIL_ARGS.search(row)
        # 分割会計：元の伝票（番号に「-」なし）に全部の注文と合計が入り、分けた分は「-1」「-2」…の伝票になる。
        # 二重に数えないよう、元の伝票だけを残す
        if '分割' in cells[3] and (re.search(r'-\d+$', slip) or (args and args.group(3) not in ('', '0'))):
            continue
        table = cells[2].split('/')[-1].strip() if '/' in cells[2] else cells[2].strip()
        out_t = re.search(r'退店\s*:?\s*\d{4}/\d{1,2}/\d{1,2}\s*(\d{1,2}:\d{2})', joined)
        in_t = re.search(r'入店\s*:?\s*\d{4}/\d{1,2}/\d{1,2}\s*(\d{1,2}:\d{2})', joined)
        day = re.search(r'営業日\s*:?\s*(\d{4})/(\d{1,2})/(\d{1,2})', joined)
        ppl = re.search(r'人数\s*:?\s*(\d+)', joined)
        tm = (out_t or in_t).group(1) if (out_t or in_t) else ''
        out.append({
            'id': 'usen-' + slip,
            'time': tm.zfill(5),
            'total': _yen(joined),
            'table': table,
            'guests': int(ppl.group(1)) if ppl else 0,
            'takeout': bool(re.search(r'テイクアウト|持ち帰り', cells[2])),
            'date': f'{day.group(1)}-{int(day.group(2)):02d}-{int(day.group(3)):02d}' if day else '',
            'items': [],
            'args': list(args.groups()) if args else None,  # 詳細を開くための値（返す前に外す）
        })
    out.sort(key=lambda o: o['time'])
    return out


def _items_from_detail(html):
    """詳細ページの「商品名・単価・数量…」の表から [{name, qty}] を作る。取消の注文は外す。"""
    for t in re.findall(r'<table.*?</table>', html, re.S):
        heads = [_text(h) for h in re.findall(r'<th[^>]*>(.*?)</th>', t, re.S)]
        if '商品名' not in heads:
            continue
        ni, qi = heads.index('商品名'), (heads.index('数量') if '数量' in heads else None)
        si = next((i for i, h in enumerate(heads) if 'ステータス' in h), None)
        items = []
        for r in re.findall(r'<tr[^>]*>(.*?)</tr>', t, re.S):
            c = [_text(x) for x in re.findall(r'<td[^>]*>(.*?)</td>', r, re.S)]
            if len(c) <= ni or not c[ni]:
                continue
            if si is not None and si < len(c) and re.search(r'取消|キャンセル', c[si]):
                continue
            q = re.sub(r'[^\d-]', '', c[qi]) if qi is not None and qi < len(c) else ''
            items.append({'name': c[ni], 'qty': int(q) if q.lstrip('-').isdigit() else 1})
        return items
    return None


def _fill_items(date_iso, list_html, orders, limit=30):
    """会計ごとの注文の中身を詳細ページから取る。会計済みの伝票は変わらないので、一度取ったら保存して使い回す。"""
    from .supabase_client import get_state, set_state
    key = f'usen:items:{date_iso}'
    cache = get_state(key) or {}
    missing = [o for o in orders if o['id'] not in cache and o.get('args')][:limit]  # 1回で開くのは最大30件（時間がかかりすぎないように。残りは次に押したときに取る）
    if missing:
        s, page = _session(), list_html
        for o in missing:
            try:
                d = _open_detail(s, page, o['args'])
            except requests.RequestException:
                break
            items = _items_from_detail(d)
            if items is None:  # 一覧に戻されたなど。次の呼び出しでやり直す
                page = _search(date_iso, date_iso)
                continue
            cache[o['id']] = items
            if 'name="voucher_number"' in d and 'csrf_token_name' in d:
                page = d  # 次の詳細は、今開いたページのフォームから開く（合言葉が新しくなっている場合に備えて）
        set_state(key, cache)
    for o in orders:
        o['items'] = cache.get(o['id'], [])


def day_sales(date_iso):
    """日報アプリ（nippo-usen.html）用。Squareの day_sales と同じ形で返す。"""
    if not is_enabled():
        return {'ok': False, 'error': 'USENレジのログイン情報が未設定です'}
    html = _search(date_iso, date_iso)
    orders = [o for o in parse_slips(html) if not o['date'] or o['date'] == date_iso]
    try:
        _fill_items(date_iso, html, orders)
    except Exception as e:  # 注文の中身が取れなくても、会計の一覧と売上は返す
        print('usen items failed:', e)
    for o in orders:
        o.pop('args', None)
    total = sum(o['total'] for o in orders)
    return {'ok': True, 'date': date_iso, 'total': total, 'count': len(orders), 'orders': orders,
            'month_total': _month_before(date_iso) + total}


def _month_before(date_iso):
    """その月の1日から前日までの合計。日中は変わらないので、日付ごとに保存して使い回す。"""
    from datetime import date, timedelta
    from .supabase_client import get_state, set_state
    d = date.fromisoformat(date_iso)
    if d.day == 1:
        return 0
    key = f'usen:month_before3:{date_iso}'  # 3: 黒伝票（会計のやり直し）も数えるようにしたあとの値
    cached = get_state(key)
    if isinstance(cached, dict) and 'total' in cached:
        return int(cached['total'])
    first, prev = d.replace(day=1).isoformat(), (d - timedelta(days=1)).isoformat()
    total = sum(o['total'] for o in parse_slips(_search(first, prev)))
    set_state(key, {'total': total})
    return total


# ---------- 伝票の詳細（注文の中身） ----------
_DETAIL_ARGS = re.compile(r"postdata\('/slip_detail/C_slip_detail/open_detail'\s*,\s*'([^']*)'\s*,\s*'([^']*)'\s*,\s*'([^']*)'\s*,\s*'([^']*)'")


def _open_detail(s, list_html, args):
    """伝票明細の一覧ページのフォームを使って、1件の詳細ページを開く。args=(店舗コード,伝票番号,枝番,営業日)"""
    data = [(k, v) for k, v in _form_defaults(list_html)
            if k not in ('my_store_code', 'voucher_number', 'voucher_branch_number', 'sum_target_date')]  # mode は画面のまま送る（元のボタンと同じ）
    data += [('my_store_code', args[0]), ('voucher_number', args[1]), ('voucher_branch_number', args[2]), ('sum_target_date', args[3])]
    return s.post(urljoin(BASE, '/slip_detail/C_slip_detail/open_detail'), data=data, timeout=40).text


def _preview(s, list_html, i=0):
    """伝票プレビュー（レシートの形）を取る。一覧の各行の slip-preview-icon-cell に付いている data-* をそのまま送る。"""
    cells = re.findall(r'<td[^>]*class="[^"]*slip-preview-icon-cell[^"]*"([^>]*)>', list_html)
    if len(cells) <= i:
        return None, []
    data = {k.replace('-', '_'): unescape(v) for k, v in re.findall(r'data-([a-z0-9_-]+)="([^"]*)"', cells[i])}
    csrf = re.search(r'name="csrf_token_name" value="([^"]*)"', list_html)
    if csrf:
        data['csrf_token_name'] = csrf.group(1)
    r = s.post(urljoin(BASE, '/electronic_journal/C_electronic_journal/get_preview_content'), data=data, timeout=40,
               headers={'X-Requested-With': 'XMLHttpRequest'})
    return r.text, sorted(data)


def probe_detail(date_iso):
    """1件目の伝票の詳細ページの「形」を返す（表の見出し・行数・数字を伏せた最初の数行）。"""
    s = _session()
    html = _search(date_iso, date_iso)
    args = _DETAIL_ARGS.findall(html)
    if not args:
        return {'ok': False, 'error': '詳細を開くリンクが見つかりません'}
    d = _open_detail(s, html, args[0])
    tables = []
    for t in re.findall(r'<table.*?</table>', d, re.S)[:6]:
        heads = [_text(h)[:20] for h in re.findall(r'<th[^>]*>(.*?)</th>', t, re.S)]
        rows = [r for r in re.findall(r'<tr[^>]*>(.*?)</tr>', t, re.S) if '<td' in r]
        sample = [[re.sub(r'\d', '#', _text(c))[:30] for c in re.findall(r'<td[^>]*>(.*?)</td>', r, re.S)] for r in rows[:4]]
        tables.append({'headers': heads[:20], 'rows': len(rows), 'sample_masked': sample})
    return {'ok': True, 'title': _text((re.search(r'<title>(.*?)</title>', d, re.S) or [None, ''])[1])[:40],
            'arg_shape': [re.sub(r'\d', '#', a) for a in args[0]], 'tables': tables,
            'labels': sorted(set(re.findall(r'(商品名|メニュー名|品名|数量|単価|金額|小計|オーダー|注文)', d))),
            'breadcrumb': _text((re.search(r'<ul class="breadcrumbs">(.*?)</ul>', d, re.S) or [None, ''])[1]),
            'main_text_masked': re.sub(r'\d', '#', _text((re.search(r'<div class="wrap[^"]*">(.*)', d, re.S) or [None, d])[1]))[:1500],
            'forms': [f['action'] for f in _forms(d, BASE)],
            **_probe_preview(s, html)}


def _probe_preview(s, html):
    pv, keys = _preview(s, html)
    if pv is None:
        return {'preview': None}
    return {'preview_keys': keys, 'preview_len': len(pv),
            'preview_text_masked': re.sub(r'\d', '#', _text(pv))[:1500]}
