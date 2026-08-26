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
    'この表は、横の並びが「日付」、縦の並びが「従業員の名前」です。',
    '交差するマスに、その人のその日の区分（A・B・P・○・×など）が入っています。',
    '',
    '出力するJSONの形式：',
    '{',
    '  "month": 月の数値（表の見出しにある月。読めなければ0）,',
    '  "days": [',
    '    {"day": 日にちの数値, "people": [{"name": "名前", "mark": "その人のマスの記号"}]}',
    '  ]',
    '}',
    '',
    'ルール：',
    '- days は表にある日付を左から順に、すべて出すこと。',
    '- people には、その日に表に載っている全員を、記号がなんであれそのまま入れること。',
    '  「×」の人も省かずに入れる（登録するかどうかはこちらで判断する）。',
    '- mark は、マスに書かれている文字をそのまま1〜2文字で書く（A、B、P、○、×など）。',
    '  空欄のマスは "" とする。セルの色は無視し、書かれている文字だけを見ること。',
    '- name は表の左端に書かれている表記のまま書く（敬称は付けない）。',
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


def read_shift(image_bytes, mime):
    """シフト表を読み取り、[{date, labels}] を日付順で返す。失敗時はNone。"""
    b64 = base64.b64encode(image_bytes).decode('ascii')
    parts = [
        {'inline_data': {'mime_type': mime, 'data': b64}},
        {'text': _PROMPT},
    ]
    data = _parse_json(call_gemini('シフト表を読み取ってJSONだけを返すアシスタント。', parts, max_tokens=4000))
    if not data:
        return None

    try:
        month = int(data.get('month') or 0)
    except (TypeError, ValueError):
        month = 0
    if not 1 <= month <= 12:
        month = config.now_jst().month
    year = _month_to_year(month)

    out = []
    for entry in (data.get('days') or [])[:MAX_DAYS]:
        if not isinstance(entry, dict):
            continue
        try:
            day = int(entry.get('day'))
        except (TypeError, ValueError):
            continue
        date_iso = _safe_date(year, month, day)
        if not date_iso:
            continue
        labels = []
        for person in entry.get('people') or []:
            if not isinstance(person, dict):
                continue
            if str(person.get('mark') or '').strip() in OFF_MARKS:
                continue          # ×＝休みなので登録しない
            label = _label(person)
            if label:
                labels.append(label)
        out.append({'date': date_iso, 'labels': labels})
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
