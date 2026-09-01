"""会議の議事録（meetings.html / 議事録ツール）。

PC側の議事録ツールが、ローカルで作った文字起こしを送ってくる。
ここで Gemini に議事録とアクションを作らせ、Supabase に保存する。

**文字起こし本文は保存しない。** Gemini に通すだけで、残すのは議事録とアクションだけ。
音声・動画はそもそもPCから出ない（議事録ツール側の設計。ここも合わせる）。
"""
import json
import re
from urllib.parse import quote

from . import config
from .gemini_client import call_gemini
from .supabase_client import get_supabase, patch_supabase, post_supabase

MINUTES_MARK = '<<<議事録>>>'
ACTIONS_MARK = '<<<アクション>>>'

# 議事録ツール側の MINUTES_PROMPT と同じ書式ルール。片方だけ直さないこと。
_SYSTEM = """あなたは会議の議事録を作る担当者です。渡されるのは自動文字起こしなので、
聞き取りの誤りが混ざっています。

# 出力の形式

次の2つの見出しを、この順で、この文言のまま出力すること。他には何も書かない。

<<<議事録>>>
（マークダウンの議事録本文。下の構成に従う）

<<<アクション>>>
（JSONの配列。下の形に従う）

# 議事録本文の構成

次の3つの見出しを必ずこの順で立てる。該当が無い見出しには「なし」と書き、省略しない。

## 1. アクション
期限が近い順の表。列は「内容 / 担当 / 期限 / 出典」。
読み取れないものは推測せず「不明」と書く。出典は [00:12:34] 形式のタイムスタンプ。

## 2. 議題
議題ごとに `### 見出し` を立て、中を
報告された事実 → 指摘・判断 → 決定事項 → アクション の順で書く。
各項目の行末に根拠となる発言のタイムスタンプ [00:12:34] を添える。

## 3. 録画で確認すべき箇所
金額・数字・固有名詞が出た箇所と、文脈が繋がらず聞き取りが怪しい箇所を、
タイムスタンプ付きで挙げ、何を確認すべきかを一言添える。

# アクションJSONの形

[{"content": "発注書を提出する", "owner": "不明", "due": "2026-09-05", "at": "00:00:40"}]

- content は「何をするか」。20文字程度の短い動詞句にする。
- owner は文字起こしから読み取れなければ "不明"。
- due は YYYY-MM-DD。「来週の金曜」のような言い方は会議日を基準に日付へ直す。
  読み取れなければ空文字 ""。
- at はその話が出たタイムスタンプ。
- アクションが無ければ [] と書く。

# 守ること

- **文字起こしに書かれていないことは書かない。** 出席者・会社名・部署名などを推測で補わない。
- 金額・固有名詞・数字は認識精度が低い。確信が持てないものには必ず「要確認」と付け、
  確定情報と混ぜない。推測で数字を埋めない。
- 前置きや感想は書かない。"""


def _split_output(text):
    """モデルの出力を議事録本文とアクションJSONに割る。

    マークダウンをJSONの文字列に入れさせると改行のエスケープでよく壊れるので、
    区切り文字で分ける形にしている。
    """
    if not text:
        return '', []
    body = text.split(MINUTES_MARK, 1)[-1]
    minutes, _, actions_part = body.partition(ACTIONS_MARK)
    minutes = minutes.strip()

    actions = []
    m = re.search(r'\[.*\]', actions_part, re.DOTALL)
    if m:
        try:
            parsed = json.loads(re.sub(r'^```(?:json)?|```$', '', m.group(0).strip(),
                                       flags=re.MULTILINE))
            if isinstance(parsed, list):
                actions = [a for a in parsed if isinstance(a, dict) and a.get('content')]
        except Exception as e:  # noqa: BLE001
            print('meetings: actions json parse failed', e)
    return minutes, actions


def ingest(source_file, transcript, title='', meeting_date=None, duration_sec=None):
    """文字起こしから議事録を作って保存する。結果の dict を返す。"""
    source_file = (source_file or '').strip()
    transcript = (transcript or '').strip()
    if not source_file:
        return {'ok': False, 'error': 'source_file が空です。'}
    if not transcript:
        return {'ok': False, 'error': '文字起こしが空です。'}

    meeting_date = meeting_date or config.today_iso()
    hint = (f'この会議が行われた日は {meeting_date} です。'
            f'「来週の金曜」のような相対的な言い方は、この日を基準に日付へ直してください。')

    # 58分の会議で約11,000文字。議事録は長くなるので出力上限は多めに取る。
    out = call_gemini(_SYSTEM, f'{hint}\n\n---\n\n{transcript}',
                      max_tokens=8000, retries=2)
    if not out:
        from .gemini_client import LAST_ERROR
        return {'ok': False, 'error': f'議事録の生成に失敗しました。{LAST_ERROR or ""}'}

    minutes, actions = _split_output(out)
    if not minutes:
        return {'ok': False, 'error': '議事録の本文を取り出せませんでした。'}

    row = {
        'title': title or source_file,
        'meeting_date': meeting_date,
        'duration_sec': duration_sec or None,
        'minutes_md': minutes,
        'actions': actions,
        'source_file': source_file,
        'updated_at': config.now_iso(),
    }

    # 同じ録画を送り直したら作り直す（議事録が気に入らないとき用）。
    existing = get_supabase('meetings',
                            f'source_file=eq.{quote(source_file)}&select=id')
    if existing:
        mid = existing[0]['id']
        if patch_supabase('meetings', f'id=eq.{quote(mid)}', row) is None:
            return {'ok': False, 'error': '議事録の更新に失敗しました。'}
    else:
        mid = config.new_id()
        if not post_supabase('meetings', [{**row, 'id': mid}]):
            return {'ok': False, 'error': '議事録の保存に失敗しました。'}

    return {'ok': True, 'id': mid, 'title': row['title'],
            'actions': actions, 'action_count': len(actions),
            'minutes_md': minutes}
