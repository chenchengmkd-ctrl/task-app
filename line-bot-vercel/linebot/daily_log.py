"""振り返りログ（日々の一言メモ）の記録。
毎晩の自動催促はいったん廃止し、こちらから送りたいときだけ「振り返り」と送る形にしている。
"""
from urllib.parse import quote

from . import config
from .supabase_client import get_supabase, post_supabase, get_state, set_state, delete_state


def add_daily_log(content):
    """「振り返り：本文」でその日の一言メモを記録する。AI処理はせずそのまま保存。"""
    if not content:
        return '振り返りの内容が空です。「振り返り：」の後に本文も送ってください。'
    ok = post_supabase('daily_logs', [{
        'log_date': config.today_iso(),
        'content': content[:500],
    }])
    if not ok:
        return '⚠️ 振り返りの記録に失敗しました。Supabaseにdaily_logsテーブルがあるか確認してください。'
    return f'📓 振り返りを記録しました\n「{content[:500]}」\n\n週次レポートやAIコーチとの相談で参考にします。'


def start_reflection_prompt():
    """「振り返り」とだけ送られた（リッチメニューのボタンを含む）ときに、次の文章を振り返りとして受け取る準備をする。"""
    set_state('PENDING_LOG_PROMPT', {'promptedAt': config.now_jst().isoformat(), 'nudged': True})
    return '📓 今日の振り返りをどうぞ。このまま普通に文章を送ればOKです。'


def handle_pending_log_reply(text):
    """「振り返り」と送られた直後の自由文を、タスク追加ではなく振り返りとして記録する。
    該当しない場合はNoneを返し、通常のルーティング（タスク追加など）に流す。
    ※明示的なコマンド（一覧・通知・番号指定など）はrouter側で先に処理されるので、ここには来ない。
    """
    pending = get_state('PENDING_LOG_PROMPT')
    if not pending:
        return None

    prompted_at_iso = pending.get('promptedAt')
    from datetime import datetime
    try:
        prompted_at = datetime.fromisoformat(prompted_at_iso)
    except (TypeError, ValueError):
        delete_state('PENDING_LOG_PROMPT')
        return None

    # 時間が経ちすぎている場合は、もう振り返りへの返信とはみなさない
    # （ここを長くすると、あとから送ったタスクが振り返りとして飲み込まれてしまう）
    if (config.now_jst() - prompted_at).total_seconds() / 3600 > 3:
        delete_state('PENDING_LOG_PROMPT')
        return None

    # すでに今日の振り返りが記録済みなら、通常のルーティングへ戻す
    since = get_supabase('daily_logs', f'created_at=gte.{quote(prompted_at_iso)}&select=id&limit=1')
    if since:
        delete_state('PENDING_LOG_PROMPT')
        return None

    reply = add_daily_log(text.strip())
    delete_state('PENDING_LOG_PROMPT')
    return reply
