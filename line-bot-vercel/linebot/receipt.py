"""LINEに送られた領収書・レシートの写真を読み取り、財務アプリの仕入れ明細として登録する。

流れ：
  写真を送る → LINEのコンテンツAPIで画像を取得 → Geminiに読み取らせる
  → 読み取り結果を一覧で返信（この時点ではまだ保存しない）
  → ユーザーが「登録」と返せば birdmen_kv に書き込む

読み取りは必ず間違えるものとして扱い、勝手に保存しない。確認してから登録する。
金額は日本のレシートの総額表示に合わせて税込で受け取る（アプリの LineItem.amount も税込）。
"""
import base64
import json
import re

import requests

from . import config
from . import finance as finance_mod
from .gemini_client import call_gemini
from .supabase_client import get_state, set_state, delete_state

PENDING_KEY = 'FINANCE_RECEIPT'
CONTENT_URL = 'https://api-data.line.me/v2/bot/message/{}/content'

# 読み取り結果の保持時間。これを過ぎた分は古い写真の残骸とみなして無視する。
# 確認待ちの間は「1削除」がレシートの行削除として扱われ、タスクの「1削除」を隠してしまうので、
# 放置された確認が長く居座らないよう短めにしてある
PENDING_TTL_MS = 15 * 60 * 1000

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


def _format_pending(pending):
    """確認用の一覧テキストを作る。"""
    items = pending['items']
    total = sum(i['amount'] for i in items)
    lines = [
        f'🧾 レシートを読み取りました（{config.jp(pending["date"])}）',
        f'仕入れ先：{pending["vendor"] or "（読み取れず）"}',
        '',
    ]
    for i, it in enumerate(items, 1):
        cat = finance_mod.CATEGORY_LABEL[it['category']]
        lines.append(f'{i}. {it["label"]}　{it["amount"]:,}円（{it["taxRate"]}% / {cat}）')
    lines += [
        '',
        f'合計 {total:,}円（税込）',
        '',
        'この内容で登録しますか？',
        '「登録」→ 保存　「取消」→ 破棄',
        '「2削除」→ 2番を除いてから登録できます',
    ]
    return '\n'.join(lines)


def handle_image(user_id, message_id):
    """画像メッセージを受け取ったときの入り口。読み取って確認メッセージを返す。"""
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

    pending = {
        'date': parsed['date'],
        'vendor': _match_vendor(parsed['vendor']),
        'items': parsed['items'],
        'ts': config.now_ms(),
    }
    set_state(f'{PENDING_KEY}:{user_id}', pending)
    return _format_pending(pending)


def _load_pending(user_id):
    pending = get_state(f'{PENDING_KEY}:{user_id}')
    if not pending or not pending.get('items'):
        return None
    if config.now_ms() - int(pending.get('ts') or 0) > PENDING_TTL_MS:
        delete_state(f'{PENDING_KEY}:{user_id}')
        return None
    return pending


def handle_pending_reply(user_id, text):
    """レシート確認待ちの返信を処理する。対象外ならNoneを返し、通常のルーティングへ流す。"""
    pending = _load_pending(user_id)
    if not pending:
        return None

    body = text.strip()

    if re.match(r'^(取消|取り消し|キャンセル|やめる|破棄)$', body):
        delete_state(f'{PENDING_KEY}:{user_id}')
        return '🗑 レシートの読み取り結果を破棄しました。'

    # 「2削除」「2,3削除」→ その行を除いてもう一度確認
    drop = re.match(r'^([\d\s,、]+)\s*(?:番)?\s*(?:削除|除外|いらない)$', body)
    if drop:
        nums = {int(n) for n in re.findall(r'\d+', drop.group(1))}
        items = [it for i, it in enumerate(pending['items'], 1) if i not in nums]
        if not items:
            delete_state(f'{PENDING_KEY}:{user_id}')
            return '全部消えたので破棄しました。'
        pending['items'] = items
        set_state(f'{PENDING_KEY}:{user_id}', pending)
        return _format_pending(pending)

    if re.match(r'^(登録|保存|ok|OK|はい|これで)$', body):
        delete_state(f'{PENDING_KEY}:{user_id}')
        return finance_mod.add_receipt_items(user_id, pending['date'], pending['vendor'], pending['items'])

    # 確認待ち中でも、関係ない文章はそのまま通常処理に流す（タスク追加などを邪魔しない）
    return None
