"""思考トレースWebアプリ（study.html）のバックエンド処理。

やること:
  - ソース（YouTube動画 / 記事URL / X・テキスト）の逐語文字起こし・要約・逐語引用の抽出
  - 取り込んだソースを横断した「思考プロファイル」の生成
  - 人物になりきった壁打ち / 訓練（差分指摘）/ 深掘り（資料に限定したQ&A）の対話

保存先は Supabase の mentors / mentor_sources / mentor_chats テーブル。
Vercelの1リクエスト60秒の制限に収めるため、動画の文字起こしはフロント側が
区間ごとに ingest_step を呼び直す方式（1回の呼び出し＝1区間）にしている。
"""
import html as _html
import json
import re
from urllib.parse import quote

import requests

from . import config
from .gemini_client import call_gemini
from .supabase_client import get_supabase, patch_supabase, post_supabase

MODEL = config.MENTOR_GEMINI_MODEL
SEGMENT_SECONDS = 600       # 動画をこの秒数ずつ区切って文字起こしする（1区間＝1リクエスト）
MAX_SEGMENTS = 36          # 安全弁（6時間ぶん）
TRANSCRIPT_BUDGET = 180_000  # 対話1回で全文をそのまま渡す合計文字数の上限
_UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36'


# ============ Supabaseヘルパー ============
def _one(table, params):
    rows = get_supabase(table, params)
    return rows[0] if rows else None


def _get_source(source_id):
    return _one('mentor_sources', f'id=eq.{quote(str(source_id))}&select=*')


def _get_mentor(mentor_id):
    return _one('mentors', f'id=eq.{quote(str(mentor_id))}&select=*')


def _patch_source(source_id, body):
    return patch_supabase(
        'mentor_sources', f'id=eq.{quote(str(source_id))}',
        {**body, 'updated_at': config.now_iso()},
    )


def _fail(source_id, message):
    _patch_source(source_id, {'status': 'error', 'error': message})
    return {'phase': 'error', 'message': message}


# ============ JSON抽出 ============
def _extract_json(text):
    """```json ...``` や前後の説明文が混ざっていても、最初のJSONオブジェクトを取り出す。"""
    if not text:
        return None
    t = re.sub(r'^```(?:json)?|```$', '', text.strip(), flags=re.MULTILINE).strip()
    try:
        return json.loads(t)
    except Exception:
        pass
    m = re.search(r'\{.*\}', t, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


# ============ 記事本文の取得 ============
def _fetch_article(url):
    try:
        r = requests.get(url, headers={'User-Agent': _UA, 'Accept-Language': 'ja,en;q=0.8'}, timeout=25)
    except requests.RequestException as e:
        print('article fetch failed', e)
        return ''
    if r.status_code != 200:
        print('article fetch status', r.status_code)
        return ''
    r.encoding = r.apparent_encoding or r.encoding
    html_text = r.text
    html_text = re.sub(r'(?is)<(script|style|nav|footer|header|aside|form|noscript)[^>]*>.*?</\1>', ' ', html_text)
    html_text = re.sub(r'(?is)<br\s*/?>', '\n', html_text)
    html_text = re.sub(r'(?is)</(p|div|li|h[1-6]|tr)>', '\n', html_text)
    text = re.sub(r'(?s)<[^>]+>', ' ', html_text)
    text = _html.unescape(text)
    text = re.sub(r'[ \t　]{2,}', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    text = re.sub(r'\n{3,}', '\n\n', text).strip()
    return text


# ============ 取り込み（フロントが phase を進めながら繰り返し呼ぶ） ============
def ingest_step(source_id, phase, index=0, text=None):
    if not source_id:
        return {'phase': 'error', 'message': 'source_id がありません。'}
    src = _get_source(source_id)
    if not src:
        return {'phase': 'error', 'message': 'ソースが見つかりません。'}

    kind = src.get('kind') or 'youtube'

    # ---- 開始：種類ごとに下ごしらえし、次のphaseを指示する ----
    if phase == 'start':
        _patch_source(source_id, {'status': 'processing', 'error': ''})

        if kind in ('text', 'x'):
            body = (text or src.get('transcript') or '').strip()
            if len(body) < 20:
                return _fail(source_id, '本文が短すぎます。もう少し長いテキストを貼り付けてください。')
            _patch_source(source_id, {'transcript': body})
            return {'phase': 'distill'}

        if kind == 'article':
            body = _fetch_article(src.get('url') or '')
            if len(body) < 200:
                return _fail(
                    source_id,
                    '記事本文を取得できませんでした（ログインが必要・JavaScript描画など）。'
                    'ページの本文をコピーして「テキスト」として貼り付けてください。',
                )
            _patch_source(source_id, {'transcript': body[:400_000]})
            return {'phase': 'distill'}

        # youtube: まず読み込めるか確認しつつ長さを聞く
        url = src.get('url') or ''
        dur_reply = call_gemini(
            'あなたは動画の長さを答えるだけのアシスタントです。',
            [{'file_data': {'file_uri': url}},
             {'text': 'この動画の長さは何秒ですか。半角数字の秒数だけを返してください（例: 3720）。'}],
            30, model=MODEL,
        )
        if dur_reply is None:
            return _fail(
                source_id,
                '動画を読み込めませんでした。非公開・限定公開・年齢制限・地域制限・ライブ配信の可能性があります。'
                'URLを確認するか、字幕付きの通常動画でお試しください。',
            )
        m = re.search(r'\d+', dur_reply.replace(',', ''))
        duration = int(m.group(0)) if m else 3600
        seg_total = max(1, min(MAX_SEGMENTS, (duration + SEGMENT_SECONDS - 1) // SEGMENT_SECONDS))
        meta = dict(src.get('meta') or {})
        meta.update({'duration': duration, 'seg_total': seg_total, 'seg_done': 0})
        _patch_source(source_id, {'transcript': '', 'meta': meta})
        return {'phase': 'transcript', 'index': 0, 'seg_done': 0, 'seg_total': seg_total}

    # ---- 動画の1区間を文字起こしして追記する ----
    if phase == 'transcript':
        url = src.get('url') or ''
        meta = dict(src.get('meta') or {})
        seg_total = int(meta.get('seg_total') or 1)
        i = int(index or 0)
        start_s = i * SEGMENT_SECONDS
        end_s = (i + 1) * SEGMENT_SECONDS
        seg = call_gemini(
            'あなたは、話し言葉をそのまま文字にする書き起こし担当です。',
            [{'file_data': {'file_uri': url},
              'video_metadata': {'start_offset': f'{start_s}s', 'end_offset': f'{end_s}s'}},
             {'text': (
                 f'この動画の {start_s} 秒〜{end_s} 秒の区間について、話されている内容を'
                 '省略・要約・言い換えを一切せず、できる限り一言一句そのまま日本語で書き起こしてください。\n'
                 '・話者が複数いる場合は「A：」「B：」のように行頭で区別する\n'
                 '・聞き取れない箇所は（不明瞭）と書く\n'
                 '・「えー」「あの」などの言いよどみは省いてよい\n'
                 '・前置きや説明、区間の要約は書かず、書き起こし本文だけを返す\n'
                 'この区間に発話が無ければ空で返してください。'
             )}],
            8192, model=MODEL,
        )
        if seg is None:
            return _fail(source_id, f'{i + 1}区間目の文字起こしに失敗しました。少し時間をおいて「続きから取り込む」で再開できます。')

        seg = seg.strip()
        prev = src.get('transcript') or ''
        combined = (prev + ('\n\n' if prev and seg else '') + seg)
        meta['seg_done'] = i + 1
        _patch_source(source_id, {'transcript': combined, 'meta': meta})

        # 実質空の区間が来たら、想定より短い動画とみなして終了
        ended = len(seg) < 15
        if i + 1 >= seg_total or ended:
            return {'phase': 'distill'}
        return {'phase': 'transcript', 'index': i + 1, 'seg_done': i + 1, 'seg_total': seg_total}

    # ---- 全文から タイトル・要約・逐語引用 を作る ----
    if phase == 'distill':
        transcript = (src.get('transcript') or '').strip()
        if len(transcript) < 20:
            return _fail(source_id, '文字起こし結果が空でした。もう一度お試しください。')

        reply = call_gemini(
            'あなたは、ある人物の発信を分析して、他の人がその思考を学ぶための教材を作る編集者です。'
            '出力は必ず指定のJSONだけにし、前後に説明文やコードフェンスを付けないこと。',
            (
                '以下はある人物の発信（動画の書き起こし、または記事本文）です。次のJSONを作ってください。\n\n'
                '{\n'
                '  "title": "内容を表す20〜30字の日本語タイトル",\n'
                '  "summary": "800字程度の日本語。単なる要約ではなく、この人物の主張・論理の運び方・'
                '判断基準・立場が伝わるように書く。ニュアンスや温度感も残す。",\n'
                '  "quotes": [\n'
                '    {"quote": "この人物の考え方・価値観・判断基準がそのまま出ている発言を原文のまま", '
                '"context": "その発言が何についてのものか一言"}\n'
                '  ]\n'
                '}\n\n'
                'quotes は8〜15個。必ず本文中の表現をそのまま抜き出す（作文しない）。\n\n'
                '=== 本文ここから ===\n' + transcript[:200_000]
            ),
            4096, model=MODEL,
        )
        data = _extract_json(reply)
        if not data:
            return _fail(source_id, '要約の生成に失敗しました。時間をおいて「続きから取り込む」で再実行してください。')

        quotes = data.get('quotes') or []
        clean_quotes = []
        for q in quotes:
            if isinstance(q, dict) and q.get('quote'):
                clean_quotes.append({'quote': str(q['quote'])[:600], 'context': str(q.get('context') or '')[:120]})
            elif isinstance(q, str):
                clean_quotes.append({'quote': q[:600], 'context': ''})

        title = str(data.get('title') or src.get('title') or '無題')[:80]
        summary = str(data.get('summary') or '')[:4000]
        _patch_source(source_id, {
            'status': 'done', 'title': title, 'summary': summary,
            'quotes': clean_quotes, 'error': '',
        })
        return {'phase': 'done', 'title': title, 'summary': summary, 'quotes': clean_quotes}

    return {'phase': 'error', 'message': f'不明なphase: {phase}'}


# ============ 思考プロファイルの生成 ============
def rebuild_profile(mentor_id):
    mentor = _get_mentor(mentor_id)
    if not mentor:
        return {'error': '人物が見つかりません。'}
    sources = get_supabase(
        'mentor_sources',
        f'mentor_id=eq.{quote(str(mentor_id))}&status=eq.done'
        '&select=title,summary,quotes,kind,url&order=created_at.asc',
    )
    if not sources:
        return {'error': '先に「ソース」を1つ以上取り込んでください。'}

    blocks = []
    for s in sources:
        qs = s.get('quotes') or []
        quote_lines = '\n'.join(f'  - 「{q.get("quote", "")}」（{q.get("context", "")}）' for q in qs)
        blocks.append(
            f'■ {s.get("title", "無題")}（{s.get("kind", "")}）\n'
            f'{s.get("summary", "")}\n'
            f'逐語引用:\n{quote_lines}'
        )
    material = '\n\n'.join(blocks)[:180_000]

    name = mentor.get('name') or 'この人物'
    profile = call_gemini(
        'あなたは、ある人物の思考様式を言語化する分析者です。'
        '読んだ人が、その人の考え方を自分の頭の中で再現できるようにすることが目的です。',
        (
            f'以下は『{name}』の発信（動画・記事）の要約と逐語引用です。これらを横断して、'
            f'『{name}』の思考のOSを次の見出しで日本語でまとめてください。'
            '憶測は避け、資料から読み取れることだけを書く。各項目、資料中の発言を引用できるところは引用する。\n\n'
            '1. 価値観・判断の軸（何を重視し、何を嫌うか）\n'
            '2. 問題の捉え方・思考の型（どういう順序で考えるか、繰り返し出てくるフレーム）\n'
            '3. 口ぐせ・言い回し・トーン\n'
            '4. 具体的な立場（「〇〇についてはこう考える」を根拠の引用つきで5つ）\n'
            '5. この人らしく判断するためのチェックリスト（10項目の箇条書き）\n\n'
            '=== 資料ここから ===\n' + material
        ),
        4096, model=MODEL,
    )
    if not profile:
        return {'error': 'プロファイルの生成に失敗しました。時間をおいて再度お試しください。'}

    updated = patch_supabase(
        'mentors', f'id=eq.{quote(str(mentor_id))}',
        {'profile': profile[:20000], 'profile_updated_at': config.now_iso()},
    )
    if updated is None:
        return {'error': 'プロファイルの保存に失敗しました。'}
    return {'profile': profile, 'source_count': len(sources)}


# ============ 対話（壁打ち / 訓練 / 深掘り） ============
_MODE_SYSTEM = {
    'spar': (
        'あなたは『{name}』本人になりきって、ユーザーと1対1で壁打ちをします。'
        '以下の「思考プロファイル」と「発信の記録」は、あなた自身の考え・発言です。'
        'これらに忠実に、あなたなら実際にどう考え、どう答え、どこに引っかかるかを述べてください。\n'
        '・人物の口調・語彙・トーンを再現する\n'
        '・ユーザーの考えの甘いところ、前提の曖昧なところには遠慮なく突っ込み、必要なら質問を返す\n'
        '・資料にないことは推測で埋めず「そこは自分の考えを言えるほど材料がない」と正直に言う\n'
        '・日本語。Markdown記法は使わず、普通の文章と行頭「・」の箇条書きで書く'
    ),
    'train': (
        'あなたは『{name}』の思考を教えるコーチです。ユーザーは自分の考えを書いてきます。'
        'あなたの仕事は次の3つです。\n'
        '1. 同じ問いに『{name}』ならどう考えるかを、プロファイルと発信の記録を根拠に示す（できれば実際の発言を引用する）\n'
        '2. ユーザーの考えとの差分を具体的に指摘する（見落としている観点、前提の違い、詰めの甘さ）\n'
        '3. 次に自分で考えるための問いを1〜2個渡す\n'
        '励ましや講評で終わらせないこと。日本語。Markdown記法は使わない。'
    ),
    'dig': (
        'あなたは『{name}』の発信アーカイブの案内役です。'
        'ユーザーの質問に、取り込まれた発信の記録（文字起こし・要約・逐語引用）だけを根拠に答えます。\n'
        '・憶測はしない。一般論で埋めない\n'
        '・答えの該当箇所は必ず「（〈タイトル〉より）「引用」」の形で示す\n'
        '・記録に無いことは「取り込まれた資料の中には見当たりません」と答える\n'
        '・日本語。Markdown記法は使わない。'
    ),
}


def chat(mentor_id, mode, message):
    mode = mode if mode in _MODE_SYSTEM else 'spar'
    if not (message or '').strip():
        return {'error': 'メッセージが空です。'}
    mentor = _get_mentor(mentor_id)
    if not mentor:
        return {'error': '人物が見つかりません。'}

    name = mentor.get('name') or 'この人物'
    profile = mentor.get('profile') or '（まだ生成されていません）'

    sources = get_supabase(
        'mentor_sources',
        f'mentor_id=eq.{quote(str(mentor_id))}&status=eq.done'
        '&select=title,kind,summary,quotes,transcript&order=created_at.desc',
    )
    if not sources:
        return {'error': '先に「ソース」を1つ以上取り込んでください。'}

    lines = []
    budget = TRANSCRIPT_BUDGET
    for s in sources:
        qs = s.get('quotes') or []
        quote_lines = '\n'.join(f'  - 「{q.get("quote", "")}」' for q in qs)
        block = f'■ {s.get("title", "無題")}（{s.get("kind", "")}）\n要約: {s.get("summary", "")}\n引用:\n{quote_lines}'
        tr = s.get('transcript') or ''
        if tr and len(tr) <= budget:
            block += f'\n--- 全文 ---\n{tr}'
            budget -= len(tr)
        lines.append(block)
    records = '\n\n'.join(lines)

    history = get_supabase(
        'mentor_chats',
        f'mentor_id=eq.{quote(str(mentor_id))}&mode=eq.{quote(mode)}'
        '&select=role,content&order=created_at.desc&limit=20',
    )
    history = list(reversed(history))
    hist_text = '\n'.join(
        f'{"ユーザー" if h.get("role") == "user" else name}：{str(h.get("content", ""))[:2000]}'
        for h in history
    )

    system = _MODE_SYSTEM[mode].format(name=name)
    prompt = (
        f'【{name} の思考プロファイル】\n{profile}\n\n'
        f'【{name} の発信の記録】\n{records}\n\n'
        f'【これまでのやり取り】\n{hist_text or "（なし）"}\n\n'
        f'【ユーザーの新しい発言】\n{message.strip()}'
    )
    reply = call_gemini(system, prompt, 1600, model=MODEL)
    if not reply:
        return {'error': 'AIが応答できませんでした。時間をおいて再度お試しください。'}

    post_supabase('mentor_chats', [
        {'id': config.new_id(), 'mentor_id': mentor_id, 'mode': mode, 'role': 'user',
         'content': message.strip()[:8000], 'created_at': config.now_iso()},
        {'id': config.new_id(), 'mentor_id': mentor_id, 'mode': mode, 'role': 'assistant',
         'content': reply[:8000], 'created_at': config.now_iso()},
    ])
    return {'reply': reply}
