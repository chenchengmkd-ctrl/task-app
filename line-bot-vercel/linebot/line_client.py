"""LINE Messaging API送信、ユーザー登録。Code.gs の replyText/pushText/rememberUser/getUsers に相当。"""
import json

import requests

from . import config
from .supabase_client import get_state, set_state


def remember_user(user_id):
    """通知用にユーザーIDを記憶する。"""
    if not user_id:
        return
    ids = get_state('USER_IDS') or []
    if user_id not in ids:
        ids.append(user_id)
        set_state('USER_IDS', ids)


def get_users():
    return get_state('USER_IDS') or []


def _messages(text):
    items = text if isinstance(text, list) else [text]
    return [{'type': 'text', 'text': t} for t in items[:5]]


def reply_text(token, text):
    """text は文字列、または複数メッセージに分けたい場合は文字列のリスト（LINEの仕様上5件まで）。"""
    res = requests.post(
        config.REPLY_URL,
        headers={
            'Authorization': f'Bearer {config.CHANNEL_ACCESS_TOKEN}',
            'Content-Type': 'application/json',
        },
        data=json.dumps({'replyToken': token, 'messages': _messages(text)}),
        timeout=20,
    )
    if res.status_code >= 300:
        print('LINE reply error', res.status_code, res.text)


def push_text(to, text):
    """LINEへのプッシュ送信。戻り値は成功したかどうか。

    失敗時はエラー内容をstateに記録する（原因調査用。/api/health から見える。
    プッシュは通知量が多い月にLINE側の無料枠上限に当たっていないかも、ここで確かめられる）。
    """
    try:
        res = requests.post(
            config.PUSH_URL,
            headers={
                'Authorization': f'Bearer {config.CHANNEL_ACCESS_TOKEN}',
                'Content-Type': 'application/json',
            },
            data=json.dumps({'to': to, 'messages': _messages(text)}),
            timeout=20,
        )
    except requests.RequestException as e:
        print('LINE push request failed', e)
        set_state('LINE_PUSH_LAST_ERROR', {'error': str(e), 'at': config.now_iso()})
        return False
    if res.status_code >= 300:
        print('LINE push error', res.status_code, res.text)
        set_state('LINE_PUSH_LAST_ERROR', {
            'status': res.status_code, 'body': res.text[:500], 'at': config.now_iso(),
        })
        return False
    # 直近の成功も記録する。エラーが出ていない＝直っている、を「そもそも何も送っていないだけ」と
    # 区別できるようにするため（/api/health から見える）
    set_state('LINE_PUSH_LAST_OK', {'at': config.now_iso()})
    return True
