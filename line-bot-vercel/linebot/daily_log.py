"""振り返りログ（日々の一言メモ）：記録・毎晩の催促・1時間後の再催促。Code.gs の該当セクションに相当。"""
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


def send_daily_log_prompt(push_text_fn, get_users_fn):
    """毎晩24時（0時）に振り返りを一言催促する。"""
    msg = '📓 今日の振り返りを一言送ってください（例：振り返り：今日はうなぎの仕込みが早く終わった）'
    for uid in get_users_fn():
        push_text_fn(uid, msg)
    set_state('PENDING_LOG_PROMPT', {'promptedAt': config.now_jst().isoformat(), 'nudged': False})


def check_daily_log_followup(push_text_fn, get_users_fn):
    """催促から1時間たっても振り返りが届いていなければ、もう一度だけ催促する（5分おきのチェックから呼ばれる）。"""
    pending = get_state('PENDING_LOG_PROMPT')
    if not pending:
        return

    prompted_at_iso = pending.get('promptedAt')
    # 催促後に振り返りが届いていれば完了（date属性のズレを避けるため created_at で判定）
    since = get_supabase('daily_logs', f'created_at=gte.{quote(prompted_at_iso)}&select=id&limit=1')
    if since:
        delete_state('PENDING_LOG_PROMPT')
        return

    if pending.get('nudged'):
        return

    from datetime import datetime
    prompted_at = datetime.fromisoformat(prompted_at_iso)
    elapsed_min = (config.now_jst() - prompted_at).total_seconds() / 60
    if elapsed_min < 60:
        return

    msg = '📓 まだ今日の振り返りが届いていません。一言だけでも送ってください（例：振り返り：今日は〜だった）'
    for uid in get_users_fn():
        push_text_fn(uid, msg)
    pending['nudged'] = True
    set_state('PENDING_LOG_PROMPT', pending)
