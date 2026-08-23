"""LINEに送られた領収書・レシートの写真を読み取り、財務アプリの仕入れ明細として登録する。

流れ：
  写真を送る → LINEのコンテンツAPIで画像を取得 → Geminiに読み取らせる → その場で登録
  → 読み取った内訳と登録結果を返信する

以前は「登録しますか？」と確認を挟み、返信を待ってから保存していたが、
毎回「登録」と返すのが手間という要望（2026-08-23）で確認を無くし即登録にした。
読み取りが間違っていた場合は「取消」で直前の1件を丸ごと戻せる（他のLINE入力と同じ仕組み）。
金額は日本のレシートの総額表示に合わせて税込で受け取る（アプリの LineItem.amount も税込）。
"""
import base64
import json
import re

import requests

from . import config
from . import finance as finance_mod
from .gemini_client import call_gemini

CONTENT_URL = 'https://api-data.line.me/v2/bot/message/{}/content'

MAX_ITEMS = 25

# base64にすると1.33倍に膨らむので、Geminiに送る前に上限を設けておく
MAX_IMAGE_BYTES = 6 * 1024 * 1024

CATEGORIES = ('ingredient', 'supplies', 'other')


def fetch_image(message_id):
    """LINEに送られた画像の実体を取得する。戻り値は (bytes, mime)。失敗時は (None, None)。"""
    try:
        res = requests.get(
            CONTENT_URL.format(message_id),
            headers={'Authorization': f'Bearer {config.CHANNEL_ACCESS_TOKEN}'},
            timeout=30,
        )
    except requests.RequestException as e:
        print('receipt fetch_image failed', e)
        return None, None
    if res.status_code != 200:
        print('receipt fetch_image error', res.status_code)
        return None, None
    mime = (res.headers.get('Content-Type') or 'image/jpeg').split(';')[0]
    return res.content, mime


_PROMPT = '\n'.join([
    'あなたは日本の飲食店の経理担当です。渡されたレシート・領収書の画像を読み取り、JSONだけを返してください。',
    '説明文・前置き・コードフェンスは一切付けないこと。',
    '',
    '出力するJSONの形式：',
    '{',
    '  "vendor": "店名（レシート上部の店舗名。読めなければ空文字）",',
    '  "date": "YYYY-MM-DD（レシートの日付。年が無ければ今年。読めなければ空文字）",',
    '  "total": 合計金額の数値（税込。読めなければ0）,',
    '  "items": [',
    '    {"label": "品名", "amount": 金額の数値, "taxRate": 8または10, "category": "ingredient|supplies|other"}',
    '  ]',
    '}',
    '',
    'ルール：',
    '- amount は必ず「税込」の金額にすること。レシートが税抜表示なら税率を掛けて税込に直す。',
    '- taxRate は軽減税率の対象（食品・飲料。酒類は除く）なら8、それ以外は10。',
    '  レシートに「※」「*」などの軽減税率マークがあればそれに従う。',
    '- category は、食材・飲食料品なら "ingredient"、消耗品や備品なら "supplies"、',
    '  それ以外（手数料・サービスなど）なら "other"。',
    '- 小計・合計・お預り・お釣り・ポイント・値引きの行は items に含めない。',
    '- 品名は略さず、レシートに書かれた表記のまま書く。',
    '- 読み取れない項目は無理に推測せず、items から省く。',
])


def _parse_json(text):
    """Geminiの応答からJSONを取り出す。コードフェンス付きで返ってくることがあるため取り除く。"""
    if not text:
        return None
    s = text.strip()
    s = re.sub(r'^```(?:json)?\s*', '', s)
    s = re.sub(r'\s*```$', '', s)
    start, end = s.find('{'), s.rfind('}')
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(s[start:end + 1])
    except (ValueError, TypeError) as e:
        print('receipt json parse failed', e, s[:200])
        return None


def _clean_items(raw):
    """Geminiの出力を、そのまま保存できる形に整える（想定外の値を弾く）。"""
    out = []
    for r in (raw or [])[:MAX_ITEMS]:
        if not isinstance(r, dict):
            continue
        label = str(r.get('label') or '').strip()
        try:
            amount = int(round(float(r.get('amount') or 0)))
        except (TypeError, ValueError):
            continue
        if not label or amount <= 0:
            continue
        # 文字列の "8" で返ってくることがあるので数値に寄せてから判定する
        try:
            rate = int(float(r.get('taxRate')))
        except (TypeError, ValueError):
            rate = 10
        if rate not in (0, 8, 10):
            rate = 10
        cat = str(r.get('category') or '').strip()
        cat = cat if cat in CATEGORIES else 'other'
        out.append({'label': label, 'amount': amount, 'taxRate': rate, 'category': cat})
    return out


def _clean_date(v):
    """YYYY-MM-DD だけ通す。読めなければ今日。"""
    m = re.match(r'^(\d{4})-(\d{1,2})-(\d{1,2})$', str(v or '').strip())
    if not m:
        return config.today_iso()
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if not (2000 <= y <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31):
        return config.today_iso()
    return f'{y:04d}-{mo:02d}-{d:02d}'


def read_receipt(image_bytes, mime):
    """画像をGeminiに読み取らせ、{vendor, date, items} を返す。失敗時はNone。"""
    b64 = base64.b64encode(image_bytes).decode('ascii')
    parts = [
        {'inline_data': {'mime_type': mime, 'data': b64}},
        {'text': f'{_PROMPT}\n\n（今日は {config.today_iso()} です）'},
    ]
    text = call_gemini('レシートを読み取ってJSONだけを返すアシスタント。', parts, max_tokens=2000)
    data = _parse_json(text)
    if not data:
        return None
    items = _clean_items(data.get('items'))
    if not items:
        return None
    return {
        'vendor': str(data.get('vendor') or '').strip(),
        'date': _clean_date(data.get('date')),
        'items': items,
    }


def _match_vendor(name):
    """読み取った店名を仕入れ先マスタの表記に寄せる。近いものが無ければ読み取ったままを使う。"""
    if not name:
        return ''
    master = finance_mod.vendor_master_names()
    for m in master:
        if m == name:
            return m
    flat = re.sub(r'[\s　株式会社（）()有限会社店]', '', name)
    for m in master:
        mf = re.sub(r'[\s　株式会社（）()有限会社店]', '', m)
        if mf and (mf in flat or flat in mf):
            return m
    return name


def _format_items(vendor, items):
    """読み取った内訳の一覧テキストを作る（登録結果の上に添える）。"""
    total = sum(i['amount'] for i in items)
    lines = [f'仕入れ先：{vendor or "（読み取れず）"}', '']
    for i, it in enumerate(items, 1):
        cat = finance_mod.CATEGORY_LABEL[it['category']]
        lines.append(f'{i}. {it["label"]}　{it["amount"]:,}円（{it["taxRate"]}% / {cat}）')
    lines += ['', f'合計 {total:,}円（税込）']
    return '\n'.join(lines)


def handle_image(user_id, message_id):
    """画像メッセージを受け取ったときの入り口。読み取ってそのまま登録し、内訳と結果を返す。"""
    image_bytes, mime = fetch_image(message_id)
    if not image_bytes:
        return '画像を取得できませんでした。もう一度送ってみてください。'
    if len(image_bytes) > MAX_IMAGE_BYTES:
        return '画像が大きすぎて読み取れませんでした。レシート部分だけを写して送ってみてください。'

    parsed = read_receipt(image_bytes, mime)
    if not parsed:
        return '\n'.join([
            'レシートの内容を読み取れませんでした。',
            '明るいところで、文字がはっきり写るように撮り直してみてください。',
            '',
            f'手で入れる場合はこちら → {finance_mod.entry_url(config.today_iso())}',
        ])

    vendor = _match_vendor(parsed['vendor'])
    result = finance_mod.add_receipt_items(user_id, parsed['date'], vendor, parsed['items'])
    return '🧾 ' + _format_items(vendor, parsed['items']) + '\n\n' + result
