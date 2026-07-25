"""参考資料（YouTube動画／テキスト）をAIコーチに学習させる。Code.gs の該当セクションに相当。"""
import re
from urllib.parse import quote

from . import config
from .gemini_client import call_gemini
from .supabase_client import get_supabase, post_supabase, patch_supabase


def add_material_from_youtube(url):
    """YouTube動画のURLを渡すと、Geminiが内容を要約して「資料」として保存する。"""
    parts = [
        {'file_data': {'file_uri': url}},
        {'text': 'この動画の内容を、後でタスクの相談・アドバイスに役立てられるよう、実務で使える要点・具体的な方法論・注意点を中心に日本語800字程度で要約してください。'
                 '1行目には「タイトル: 〇〇」の形式で20文字程度の短いタイトルだけを入れ、2行目以降に要約本文を書いてください。'},
    ]
    reply = call_gemini('あなたはタスク管理アシスタントのための資料要約担当です。', parts, 800)
    if not reply:
        return '⚠️ 動画の読み込みに失敗しました。URLが正しいか確認のうえ、時間をおいて再度お試しください。'
    return save_material(reply, 'youtube', url)


def add_material_from_text(text):
    """貼り付けられたテキストを、後で参照しやすい形に整理して「資料」として保存する。"""
    if not text:
        return '資料の内容が空です。「資料：」の後に本文も送ってください。'
    reply = call_gemini(
        'あなたはタスク管理アシスタントのための資料要約担当です。渡されたテキストの1行目に「タイトル: 〇〇」の形式で20文字程度の短いタイトルをつけてください。'
        '2行目以降に、後でタスクの相談・アドバイスに役立てられるよう要点を日本語600字程度で整理してください。元のテキストがすでに簡潔なら、大きく書き換えずそのまま活かして構いません。',
        text, 600,
    )
    if not reply:
        return '⚠️ 資料の整理に失敗しました。時間をおいて再度お試しください。'
    return save_material(reply, 'text', None)


def save_material(ai_text, source_type, source_url):
    """AIの要約結果（1行目「タイトル: 〇〇」＋本文）をパースしてSupabaseに保存する。"""
    m = re.search(r'^タイトル[:：]\s*(.+)$', ai_text, re.MULTILINE)
    title = m.group(1).strip() if m else ai_text[:20]
    summary = ai_text.replace(m.group(0), '').strip() if m else ai_text

    ok = post_supabase('materials', [{
        'title': title[:60],
        'source_type': source_type,
        'source_url': source_url,
        'summary': summary[:1500],
        'created_at': config.now_iso(),
    }])
    if not ok:
        return '⚠️ 資料の保存に失敗しました。Supabaseにmaterialsテーブルがあるか確認してください。'

    return (
        f'📚 資料として覚えました\n「{title}」\n\n{summary}'
        '\n\n内容が違う場合は「〇〇の資料を△△に直して」のように送れば修正できます。'
    )


def list_materials():
    """登録済みの資料一覧を表示する。"""
    rows = get_supabase('materials', 'select=title,source_type&order=created_at.desc&limit=20')
    if not rows:
        return '📚 登録済みの資料はまだありません。「資料：本文」やYouTubeのURLを送ると覚えます。'
    lines = [f"・{r['title']}{'🎥' if r.get('source_type') == 'youtube' else '📝'}" for r in rows]
    return '📚 資料一覧\n' + '\n'.join(lines)


def handle_update_material(input_args, intro):
    """登録済み資料の内容修正を実行（タイトルが一意に特定できる場合のみ）。"""
    a = input_args or {}
    title = str(a.get('material_title') or '').strip()
    instruction = str(a.get('instruction') or '').strip()
    if not title or not instruction:
        return '⚠️ 資料の修正内容を理解できませんでした。'

    matches = get_supabase('materials', f'title=eq.{quote(title)}&select=id,title,summary')
    if not matches:
        return f'⚠️「{title}」という資料が見つかりませんでした。「資料一覧」で確認してください。'
    if len(matches) > 1:
        return f'⚠️「{title}」に一致する資料が複数あります。「資料一覧」で確認してください。'

    target = matches[0]
    reply = call_gemini(
        'あなたはタスク管理アシスタントのための資料要約担当です。以下は現在保存されている資料の要約です。ユーザーの修正指示に従って書き換えてください。'
        '1行目に「タイトル: 〇〇」の形式で20文字程度の短いタイトルをつけてください（変更不要ならそのまま）。2行目以降に、修正後の要約を日本語600字程度で書いてください。',
        f"【現在の要約】\n{target['summary']}\n\n【修正指示】\n{instruction}",
        600,
    )
    if not reply:
        return '⚠️ 資料の修正に失敗しました。時間をおいて再度お試しください。'

    m = re.search(r'^タイトル[:：]\s*(.+)$', reply, re.MULTILINE)
    new_title = m.group(1).strip() if m else target['title']
    new_summary = reply.replace(m.group(0), '').strip() if m else reply

    updated = patch_supabase('materials', f"id=eq.{quote(str(target['id']))}", {
        'title': new_title[:60], 'summary': new_summary[:1500],
    })
    if updated is None:
        return f'⚠️「{title}」の修正に失敗しました。'

    prefix = f'{intro}\n\n' if intro else ''
    return f'{prefix}📚「{new_title}」を修正しました\n\n{new_summary}'
