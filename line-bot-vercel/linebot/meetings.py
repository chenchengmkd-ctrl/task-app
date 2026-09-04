"""会議の議事録（meetings.html / 議事録ツール）。

PC側の議事録ツールから、次のどちらかが送られてくる。

1. 文字起こしだけ  … ここで Gemini に議事録を作らせる（毎朝の自動処理向け。手間ゼロ）
2. 議事録つき      … claude.ai で作った議事録をそのまま保存する（品質重視。こちらが本命）

2 を用意しているのは、要約の質がモデルの差でそのまま出るため。
サーバーの Gemini は無料枠の軽いモデルなので、深い読み解きは claude.ai 側にやらせて、
ここは「保存して、スマホから読めるようにする」役に徹する。

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

# 議事録ツール側の MINUTES_PROMPT と揃えること。片方だけ直さない。
_SYSTEM = """あなたは会議の議事録を作る担当者です。渡されるのは自動文字起こしなので、
聞き取りの誤りが混ざっています。

# 出力の形式

次の2つの見出しを、この順で、この文言のまま出力すること。他には何も書かない。

<<<議事録>>>
（マークダウンの議事録本文）

<<<アクション>>>
（JSONの配列）

# 議事録本文の構成

## 1. アクション
期限が近い順の表。列は「内容 / 担当 / 期限 / 出典」。出典は [00:12:34] 形式。

## 2. 議題
**話された議題の数だけ `### 見出し` を立てること。** 3つにまとめようとしない。
雑談以外で数分以上話された話題は、すべて独立した議題として立てる。
各議題の中は、内容に応じて次を使い分ける。

- **報告された内容** … 誰が何を報告したか。数字・固有名詞はここに全部残す
- **指摘・判断** … 誰がどう指摘したか。**要約して薄めないこと。**
  「原価管理が甘い」ではなく、指摘の中身（何と何を比べるべきか、なぜそう言えるのか）を書く
- **決定事項** … 決まったこと
- **背景** … なぜそうするのか、前提となる事情が語られていれば書く

各項目の行末に、根拠となる発言のタイムスタンプ [00:12:34] を添える。

## 3. その他
議題として立てるほどではないが、記録しておくべき事項（予定、連絡手段の取り決めなど）。
無ければこの見出しごと省略してよい。

## 4. 録画で確認すべき箇所
金額・数字・固有名詞が出た箇所と、文脈が繋がらず聞き取りが怪しい箇所。
タイムスタンプ付きで挙げ、何を確認すべきかを添える。

# アクションJSONの形

[{"content": "八月の収支を全項目積み上げてスプレッドシートにまとめる",
  "owner": "誠", "due": "2026-09-01", "at": "00:40:24"}]

- content は具体的に。何を・どこまでやるのかが分かる長さにする（短くしすぎない）。
- due は YYYY-MM-DD。「明日」「来週の金曜」は会議日を基準に日付へ直す。
  「9月中」のように幅がある言い方は、その月末の日付にする。読み取れなければ ""。
- at はその話が出たタイムスタンプ。
- アクションが無ければ []。

# 守ること

- **数字と固有名詞は落とさない。** 金額・個数・日付・商品名・人名・場所は、
  聞き取りが怪しくても**そのまま書いたうえで「要確認」を付ける**。
  自信が無いことを理由に省略してはいけない。省略されると後から確認もできなくなる。
- **担当は文脈から特定してよい。** 出席者が2名なら、どちらの発言か・どちらがやるのかは
  会話の流れで分かることが多い。分かるなら名前を書く。本当に判別できないときだけ "不明"。
- ただし**文字起こしに無い事実は作らない。** 会社名・日付・出席者名などを、
  会話に出ていないのに補ってはいけない。推測と事実を混ぜない。
- 前置きや感想は書かない。"""

# 既にできあがった議事録からアクションだけ抜くための指示。
# 読み解きは終わっている前提なので、軽いモデルでも十分こなせる。
_ACTIONS_SYSTEM = """渡されるのは完成した会議の議事録です。
そこに書かれているアクション（誰かがこれからやること）をJSONの配列で抜き出してください。

[{"content": "八月の収支をスプレッドシートにまとめる",
  "owner": "誠", "due": "2026-09-01", "at": "00:40:24"}]

- content は議事録の表現をなるべくそのまま使う。
- owner は議事録から読み取れなければ "不明"。
- due は YYYY-MM-DD。「明日」のような相対的な表記は会議日を基準に直す。
  「9月中」のように幅があるなら月末の日付。読み取れなければ ""。
- at はタイムスタンプ。議事録に無ければ ""。
- JSONの配列だけを出力する。説明文は書かない。アクションが無ければ []。"""


def _model():
    """議事録用のモデル。MEETING_GEMINI_MODEL が未設定なら通常の GEMINI_MODEL。

    既定の gemini-3.1-flash-lite は LINE の短い応答用に選ばれた軽いモデルで、
    58分の会議を深く読み解くには力不足。無料枠でより強いモデルが使えるなら、
    Vercel の環境変数 MEETING_GEMINI_MODEL で上書きする。
    """
    return config.MEETING_GEMINI_MODEL


def _parse_actions(text):
    if not text:
        return []
    m = re.search(r'\[.*\]', text, re.DOTALL)
    if not m:
        return []
    try:
        parsed = json.loads(re.sub(r'^```(?:json)?|```$', '', m.group(0).strip(),
                                   flags=re.MULTILINE))
    except Exception as e:  # noqa: BLE001
        print('meetings: actions json parse failed', e)
        return []
    if not isinstance(parsed, list):
        return []
    return [a for a in parsed if isinstance(a, dict) and a.get('content')]


def _split_output(text):
    """モデルの出力を議事録本文とアクションJSONに割る。

    マークダウンをJSONの文字列に入れさせると改行のエスケープでよく壊れるので、
    区切り文字で分ける形にしている。
    """
    if not text:
        return '', []
    body = text.split(MINUTES_MARK, 1)[-1]
    minutes, _, actions_part = body.partition(ACTIONS_MARK)
    return _clean_md(minutes), _parse_actions(actions_part)


def _clean_md(md):
    """モデルが時々まぜてくる生の「\\n」（改行のつもりの文字列）を実際の改行に直す。
    見出しの末尾に付くと `## 2. 議題\\n` のように表示が崩れる。議事録に
    バックスラッシュが正当に出ることはないので、まるごと置き換えてよい。
    """
    return (md or '').replace('\\n', '\n').strip()


_COACH_TAG = '#会議'


def _feed_meeting_mentors(title, meeting_date, minutes):
    """思考トレース（study.html）のメンターに、この会議での指摘を自動で足す。

    対象は mentors テーブルで note に「#会議」を含む人物。study.html の「人物設定」で
    メモ欄に #会議 と書いておくと、以降の議事録からその人の発言だけが抽出されて
    ソースになる。

    ここでは**抽出とソース登録まで**（Geminiを1回だけ）。要約・逐語引用・プロファイル
    再生成は時間がかかる（Vercelの60秒制限）ので、PC側が続けて
    /api/meeting_mentor_finish を呼んで仕上げる。

    失敗は握りつぶす（議事録の保存を巻き込まない）。
    仕上げ待ちの {name, mentor_id, source_id} のリストを返す。
    """
    try:
        rows = get_supabase('mentors', 'select=id,name,note')
    except Exception as e:  # noqa: BLE001
        print('meetings: mentors 取得に失敗', e)
        return []
    targets = [m for m in rows if _COACH_TAG in (m.get('note') or '')]
    if not targets:
        return []

    fed = []
    for m in targets:
        name = m.get('name') or ''
        bare = re.sub(r'(さん|くん|君|様|さま|氏)$', '', name)
        extracted = call_gemini(
            f'あなたは会議の議事録から、特定の人物（{name}）の考え方だけを取り出す担当です。',
            (f'次の議事録から、{name}（{bare}）がした指摘・助言・判断・問いかけを、'
             f'本人の論理と言い回しをなるべく残して箇条書きで書き出してください。\n'
             f'・会議の決定事項、事実報告、他の人の発言は含めない\n'
             f'・{name} が「なぜそう言うのか」「何と何を比べているのか」が分かるように書く\n'
             f'・繰り返し出てくる考え方の型があれば、それも書く\n'
             f'該当する発言が議事録に無ければ「なし」とだけ返す。\n\n'
             f'--- 議事録（{title} / {meeting_date}）---\n\n{minutes}'),
            4000, model=_model(), retries=2)
        if not extracted or extracted.strip() in ('なし', '') or len(extracted.strip()) < 40:
            continue

        sid = config.new_id()
        row = {
            'id': sid, 'mentor_id': m['id'], 'kind': 'text',
            'title': f'会議: {title}（{meeting_date}）',
            'transcript': extracted.strip(), 'status': 'processing',
            'meta': {'from_meeting': True},
        }
        if not post_supabase('mentor_sources', [row]):
            continue
        fed.append({'name': name, 'mentor_id': m['id'], 'source_id': sid})
    return fed


def finish_mentor_source(source_id, mentor_id=None):
    """会議から作ったメンターのソースを仕上げる。要約・逐語引用 → プロファイル再生成。
    PC側から /api/meeting_mentor_finish 経由で、議事録保存の直後に呼ばれる。
    """
    from . import mentors as mentors_mod
    out = {'source_id': source_id}
    try:
        step = mentors_mod.ingest_step(source_id, 'distill')
        out['distill'] = step.get('phase')
    except Exception as e:  # noqa: BLE001
        out['distill_error'] = repr(e)
    if mentor_id:
        try:
            r = mentors_mod.rebuild_profile(mentor_id)
            out['profile'] = 'ok' if r.get('profile') else r.get('error')
        except Exception as e:  # noqa: BLE001
            out['profile_error'] = repr(e)
    return out


def _notify_line(title, action_count, fed_mentors=None):
    """議事録ができたことをLINEで知らせる。中身は羅列せず、件数とアプリのリンクだけ。"""
    try:
        from .line_client import get_users, push_text
        text = (f'🗒 議事録ができました\n{title}\n'
                f'アクション{action_count}件\n\n'
                f'{config.APP_URL}meetings.html')
        if fed_mentors:
            names = "・".join(f.get('name', '') for f in fed_mentors)
            text += f'\n\n🧠 {names} の思考トレースにも追加しました'
        for uid in get_users():
            push_text(uid, text)
    except Exception as e:  # noqa: BLE001
        print('meetings: LINE通知に失敗', e)


def ingest(source_file, transcript, title='', meeting_date=None, duration_sec=None,
           minutes_md=None, notify=False):
    """議事録を保存する。結果の dict を返す。

    minutes_md が渡されていればそれをそのまま保存し、アクションだけ抜き出す
    （claude.ai で作った議事録を持ち込む経路）。
    渡されていなければ、文字起こしから Gemini で議事録を作る。
    """
    source_file = (source_file or '').strip()
    transcript = (transcript or '').strip()
    minutes_md = (minutes_md or '').strip()
    if not source_file:
        return {'ok': False, 'error': 'source_file が空です。'}
    if not transcript and not minutes_md:
        return {'ok': False, 'error': '文字起こしも議事録も空です。'}

    meeting_date = meeting_date or config.today_iso()
    hint = (f'この会議が行われた日は {meeting_date} です。'
            f'「明日」「来週の金曜」のような相対的な言い方は、この日を基準に日付へ直してください。')

    if minutes_md:
        # 読み解きは済んでいる。ここでやるのはアクションの抜き出しだけ。
        out = call_gemini(_ACTIONS_SYSTEM, f'{hint}\n\n---\n\n{minutes_md}',
                          max_tokens=3000, model=_model(), retries=2)
        actions = _parse_actions(out)
        minutes = minutes_md
    else:
        out = call_gemini(_SYSTEM, f'{hint}\n\n---\n\n{transcript}',
                          max_tokens=8000, model=_model(), retries=2)
        if not out:
            from .gemini_client import LAST_ERROR
            return {'ok': False, 'error': f'議事録の生成に失敗しました。{LAST_ERROR or ""}'}
        minutes, actions = _split_output(out)
        if not minutes:
            return {'ok': False, 'error': '議事録の本文を取り出せませんでした。'}
        if not actions:
            # 議事録は書けたのに末尾のJSONを出し損ねることがある（実際に起きた）。
            # 本文には「## 1. アクション」の表が入っているので、そこから抜き直す。
            actions = _parse_actions(
                call_gemini(_ACTIONS_SYSTEM, f'{hint}\n\n---\n\n{minutes}',
                            max_tokens=3000, model=_model(), retries=2))

    row = {
        'title': title or source_file,
        'meeting_date': meeting_date,
        'duration_sec': duration_sec or None,
        'minutes_md': minutes,
        'actions': actions,
        'source_file': source_file,
        'updated_at': config.now_iso(),
    }

    # 同じ録画を送り直したら上書きする（議事録を作り直したいとき用）。
    existing = get_supabase('meetings', f'source_file=eq.{quote(source_file)}&select=id')
    if existing:
        mid = existing[0]['id']
        if patch_supabase('meetings', f'id=eq.{quote(mid)}', row) is None:
            return {'ok': False, 'error': '議事録の更新に失敗しました。'}
    else:
        mid = config.new_id()
        if not post_supabase('meetings', [{**row, 'id': mid}]):
            return {'ok': False, 'error': '議事録の保存に失敗しました。'}

    fed = _feed_meeting_mentors(row['title'], meeting_date, minutes)

    if notify:
        _notify_line(row['title'], len(actions), fed)

    return {'ok': True, 'id': mid, 'title': row['title'],
            'actions': actions, 'action_count': len(actions),
            'minutes_md': minutes, 'pasted': bool(minutes_md),
            'mentors_fed': fed}
