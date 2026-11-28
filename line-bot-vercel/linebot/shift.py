"""シフト表の写真を読み取って、日ごとの担当をGoogleカレンダーに入れる。

流れ：
  半月ぶんのシフト表の写真を送る → 画像を取得 → Geminiに表を読ませる
  → 日ごとの担当を一覧で返信（この時点ではまだ登録しない）
  → 「はい」と返せばカレンダーに書き込む

読み取りは必ず間違えるものとして扱い、勝手に登録しない。16日ぶんをいきなり
書き込んで間違っていると直すのが大変なため、必ず確認を挟む。

予定は毎日 3:00〜4:00 に置く。実際の用事とぶつからない時間に集めて、
その日の担当を一目で確認するための枠として使う。
"""
import base64
import json
import os
import re

from . import config
from . import gcal
from .gemini_client import call_gemini
from .supabase_client import get_state, set_state, delete_state

PENDING_KEY = 'SHIFT_IMPORT'
PENDING_TTL_MS = 30 * 60 * 1000     # 確認待ちの有効時間

START_HHMM = '03:00'
END_HHMM = '04:00'

# 予定の説明欄に入れる印。送り直したときに「自分で入れたぶんだけ」消すために使う。
# 手で入れた予定を巻き込んで消さないための唯一の手がかりなので、文字列は変えないこと。
MARKER = '[シフト自動登録]'

# 「×」は休みなので登録しない。それ以外（A・B・P・○）は登録する。
OFF_MARKS = {'×', 'x', 'X', '✕', '✖', '－', '-', ''}
# ○ は記号として意味を持たないので、名前だけを出す
PLAIN_MARKS = {'○', '◯', '〇', 'O', 'o'}

MAX_DAYS = 40           # 半月ぶん＋余裕。これを超えたら読み取り失敗とみなす
MAX_IMAGE_BYTES = 6 * 1024 * 1024

_PROMPT = '\n'.join([
    'あなたは日本の飲食店のシフト管理担当です。渡されたシフト表の画像を読み取り、JSONだけを返してください。',
    '説明文・前置き・コードフェンスは一切付けないこと。',
    '',
    'この表は、一番上の行に「日付」が左から右へ並び、左端の列に「従業員の名前」が上から下へ並んでいます。',
    '名前の行と日付の列が交差するマスに、その人のその日の区分（A・B・C・P・○・×など）が入っています。',
    '表の右側にある凡例（「A 09:30-13:30」のような時間の説明）は表ではないので無視すること。',
    '',
    '読み方（必ずこの順で）：',
    '1. 一番上の行の日付を、左端から右端まで1つ残らず数字で列挙する（例：11/16 なら 16）。',
    '2. 名前ごとに1行ずつ、その行のマスを左端の日付から右端の日付まで、1マスも飛ばさず順に読む。',
    '   marks の個数は、必ず手順1の日付の個数と同じにすること。',
    '   隣の行や隣の列と取り違えないよう、名前の行を横にまっすぐたどること。',
    '',
    '出力するJSONの形式：',
    '{',
    '  "month": 表の見出しや日付にある月の数値（読めなければ0）,',
    '  "dates": [日にちの数値, ...],',
    '  "rows": [',
    '    {"name": "名前", "marks": ["左端の日付のマスの記号", "2番目の日付のマスの記号", ...]}',
    '  ]',
    '}',
    '',
    'ルール：',
    '- mark は、マスに書かれている文字をそのまま1〜2文字で書く（A、B、C、P、○、×など）。大文字小文字は区別しなくてよい。',
    '  空欄のマスは "" とする。セルの色は無視し、書かれている文字だけを見ること。',
    '- name は表の左端に書かれている表記のまま書く（敬称は付けない）。表にいる全員を、×ばかりの人も含めて出す。',
    '- 表に無い日付・人を推測で足さないこと。',
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
    except ValueError:
        return None


def looks_like_shift_table(image_bytes, mime):
    """この画像がシフト表かどうかを先に見分ける。レシートの読み取りと取り違えないため。"""
    b64 = base64.b64encode(image_bytes).decode('ascii')
    parts = [
        {'inline_data': {'mime_type': mime, 'data': b64}},
        {'text': 'この画像は、横に日付が並び縦に人名が並んだ「シフト表（勤務表）」ですか？'
                 'シフト表なら shift、レシート・領収書なら receipt、どちらでもなければ other と、'
                 '1語だけ返してください。'},
    ]
    reply = call_gemini('画像の種類を1語で答えるアシスタント。', parts, max_tokens=10)
    return bool(reply) and 'shift' in reply.strip().lower()


def _month_to_year(month):
    """表には年が書かれていないので、いまの月から推測する。
    12月に「1月」の表を受け取る場合があるので、月がだいぶ戻っていたら翌年とみなす。
    """
    now = config.now_jst()
    if month and month < now.month - 6:
        return now.year + 1
    if month and month > now.month + 6:
        return now.year - 1
    return now.year


def _label(person):
    """「かなA」「向井P」のような表示。○ は記号を出さず名前だけにする。"""
    name = str(person.get('name') or '').strip()
    mark = str(person.get('mark') or '').strip()
    if not name:
        return ''
    if mark in PLAIN_MARKS:
        return name
    return f'{name}{mark}'


# 読み取りに使うモデル。強いものから順に試し、使えない（存在しない・無料枠外）なら次へ。
# 既定の GEMINI_MODEL（flash-lite）は軽すぎて、横に長い表で列を取り違える（11/24以降が1列ずれる等）。
SHIFT_MODELS = [m for m in [os.environ.get('SHIFT_GEMINI_MODEL', '').strip(),
                            'gemini-3.1-flash', 'gemini-3-flash-preview', 'gemini-2.5-flash',
                            config.GEMINI_MODEL] if m]
_good_model = None


def _ask(parts):
    """使えるモデルが見つかるまで順に試す。→ 応答テキスト（だめならNone）"""
    global _good_model
    from . import gemini_client
    order = ([_good_model] if _good_model else []) + [m for m in SHIFT_MODELS if m != _good_model]
    for m in order:
        text = call_gemini('シフト表を読み取ってJSONだけを返すアシスタント。', parts, max_tokens=8000, model=m, retries=1)
        if text:
            _good_model = m
            return text
        err = gemini_client.LAST_ERROR or ''
        if not any(k in err for k in ('HTTP 404', 'HTTP 400', 'HTTP 403', 'not found', 'NOT_FOUND', 'not supported')):
            return None       # モデルの問題ではない失敗（混雑など）は、別モデルに替えても同じなので打ち切る
    return None


def _read_table(parts):
    """1回読む。→ {'month', 'dates', 'rows'}（日付と各行の個数がそろっていない行は bad に入れる）。失敗時None"""
    data = _parse_json(_ask(parts))
    if not data:
        return None
    dates = []
    for d in data.get('dates') or []:
        try:
            dates.append(int(str(d).split('/')[-1]))
        except (TypeError, ValueError):
            return None
    if not dates or len(dates) > MAX_DAYS:
        return None
    rows, bad = [], []
    for r in data.get('rows') or []:
        if not isinstance(r, dict) or not str(r.get('name') or '').strip():
            continue
        marks = [str(x or '').strip() for x in (r.get('marks') or [])]
        (rows if len(marks) == len(dates) else bad).append({'name': str(r['name']).strip(), 'marks': marks})
    try:
        month = int(data.get('month') or 0)
    except (TypeError, ValueError):
        month = 0
    return {'month': month, 'dates': dates, 'rows': rows, 'bad': bad}


def _working(table, i):
    """その日に出る人 [{name, mark}]"""
    return [{'name': r['name'], 'mark': r['marks'][i]} for r in table['rows'] if r['marks'][i] not in OFF_MARKS]


def read_shift(image_bytes, mime, names_only=False):
    """シフト表を読み取り、[{date, labels, unsure}] を日付順で返す。失敗時はNone。
    同じ画像を2回読み、食い違った日・読み切れなかった行がある日は unsure=True にする（人の目で確かめてもらう）。
    names_only=True のときは記号（A・B・P）を付けず名前だけにする（定期報告アプリの予定タブ用）。"""
    from concurrent.futures import ThreadPoolExecutor
    b64 = base64.b64encode(image_bytes).decode('ascii')
    parts = [{'inline_data': {'mime_type': mime, 'data': b64}}, {'text': _PROMPT}]
    with ThreadPoolExecutor(max_workers=2) as ex:
        results = [t for t in ex.map(lambda _: _read_table(parts), range(2)) if t and t['rows']]
    if not results:
        return None
    main = max(results, key=lambda t: (len(t['rows']), -len(t['bad'])))
    other = next((t for t in results if t is not main), None)

    month = main['month'] if 1 <= main['month'] <= 12 else config.now_jst().month
    year = _month_to_year(month)
    out, prev_day, cur_month, cur_year = [], None, month, year
    for i, day in enumerate(main['dates']):
        if prev_day is not None and day < prev_day:      # 月をまたぐ表（11/30 → 12/1）
            cur_month = cur_month % 12 + 1
            cur_year += cur_month == 1
        prev_day = day
        date_iso = _safe_date(cur_year, cur_month, day)
        if not date_iso:
            continue
        people = _working(main, i)
        unsure = bool(main['bad'])
        if other is not None:
            if other['dates'] != main['dates']:
                unsure = True
            else:
                a = sorted(p['name'] for p in people)
                b = sorted(p['name'] for p in _working(other, i))
                unsure = unsure or a != b
        labels = [p['name'] if names_only else _label(p) for p in people]
        out.append({'date': date_iso, 'labels': [x for x in labels if x], 'unsure': unsure})
    out.sort(key=lambda d: d['date'])
    return out or None


def _safe_date(year, month, day):
    from datetime import datetime
    try:
        return config.iso_of_date(datetime(year, month, day, tzinfo=config.JST))
    except ValueError:
        return None


def _format_pending(days):
    """登録前の確認用の一覧。担当がいない日も「休み」として見せる（読み落としに気づけるように）。"""
    working = [d for d in days if d['labels']]
    head = (f'📋 シフト表を読み取りました（{config.jp(days[0]["date"])}〜{config.jp(days[-1]["date"])}）\n'
            f'出勤あり {len(working)}日 / 全{len(days)}日\n')
    lines = []
    for d in days:
        dt = config.parse_date(d['date'])
        wd = '日月火水木金土'[(dt.weekday() + 1) % 7]
        body = '、'.join(d['labels']) if d['labels'] else '（休み）'
        if d.get('unsure'):
            body += '　⚠要確認'
        lines.append(f'{dt.day}日({wd}) {body}')
    return (head + '\n' + '\n'.join(lines) +
            f'\n\nこの内容で、毎日 {START_HHMM}〜{END_HHMM} の枠にカレンダー登録しますか？'
            '\n→ 「はい」で登録、「いいえ」で取り消し'
            '\n→ 読み間違いがあれば「14日 都丸、上原」のように送るとその日だけ直せます（休みなら「14日 休み」）'
            '\n（同じ期間にこの機能で入れた予定があれば、消してから入れ直します）')


def handle_image(user_id, image_bytes, mime):
    """シフト表の画像を読み取り、確認待ちにする。→ 返信文"""
    days = read_shift(image_bytes, mime)
    if not days:
        return ('⚠️ シフト表として読み取れませんでした。\n'
                '表全体が入るように、明るいところで撮り直して送ってください。')
    set_state(_key(user_id), {'days': days, 'createdAt': config.now_ms()})
    return _format_pending(days)


def _key(user_id):
    return f'{PENDING_KEY}_{user_id}'


def _load_pending(user_id):
    pending = get_state(_key(user_id))
    if not pending:
        return None
    if (config.now_ms() - pending.get('createdAt', 0)) > PENDING_TTL_MS:
        delete_state(_key(user_id))
        return None
    return pending


def _clear_previous(date_iso):
    """その日にこの機能で入れた予定を消す。→ 消した件数
    印（MARKER）が入っているものだけを対象にして、手で入れた予定は触らない。
    """
    removed = 0
    for ev in gcal.get_events_with_id(date_iso):
        if MARKER not in (ev.get('description') or ''):
            continue
        ok, _ = gcal.delete_event(ev['id'])
        if ok:
            removed += 1
    return removed


def register(user_id):
    """確認済みのシフトをカレンダーに書き込む。→ 返信文"""
    pending = _load_pending(user_id)
    if not pending:
        return None
    days = pending.get('days') or []
    delete_state(_key(user_id))
    if not days:
        return '⚠️ 登録する内容がありませんでした。もう一度写真を送ってください。'

    added, replaced, failed = 0, 0, []
    for d in days:
        replaced += _clear_previous(d['date'])
        if not d['labels']:
            continue          # 全員休みの日は予定を作らない
        title = '、'.join(d['labels'])
        ok, err = gcal.create_event(
            title, d['date'], START_HHMM, END_HHMM,
            description=f'{MARKER}\nLINEに送られたシフト表から自動で作成しました。',
        )
        if ok:
            added += 1
        else:
            failed.append(f"{config.jp(d['date'])}：{err}")

    msg = f'✅ カレンダーに登録しました（{added}日ぶん）'
    if replaced:
        msg += f'\n（前に登録していた {replaced}件 は消して入れ直しました）'
    if failed:
        msg += '\n\n⚠️ 登録できなかった日\n' + '\n'.join(failed[:5])
        if len(failed) > 5:
            msg += f'\n…ほか{len(failed) - 5}日'
    return msg


_CORRECT_RE = re.compile(r'^(\d{1,2})日[\s、,:：]+(.+)$')


def _apply_correction(user_id, pending, text):
    """確認待ち中に「14日 都丸、上原」のように送ると、その日だけ内容を上書きしてもう一度確認を出す。
    範囲外の日付・パターンに合わないものはNone（通常のルーティングに任せる）。
    """
    m = _CORRECT_RE.match(text.strip())
    if not m:
        return None
    day_num = int(m.group(1))
    body = m.group(2).strip()
    days = pending['days']
    target = next((d for d in days if config.parse_date(d['date']).day == day_num), None)
    if not target:
        return None
    target['labels'] = [] if re.match(r'^(休み|なし|やすみ)$', body) else \
        [x.strip() for x in re.split('[、,]', body) if x.strip()]
    set_state(_key(user_id), pending)
    return _format_pending(days)


def handle_pending_reply(user_id, text):
    """確認待ちへの「はい」「いいえ」／個別日の訂正を処理する。該当なしはNone。"""
    pending = _load_pending(user_id)
    if not pending:
        return None
    t = text.strip()
    if re.match(r'^(はい|うん|ok|OK|登録|お願いします|よろしく|そう)[。.!！]*$', t, re.IGNORECASE):
        return register(user_id)
    if re.match(r'^(いいえ|やめて|キャンセル|違う|ちがう|no)[。.!！]*$', t, re.IGNORECASE):
        delete_state(_key(user_id))
        return '🙅 シフトの登録をやめました。'
    return _apply_correction(user_id, pending, t)
