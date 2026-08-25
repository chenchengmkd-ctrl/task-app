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
    """LINEへのプッシュ送信。戻り値は成功したかどうか（呼び出し元が失敗を検知して記録できるように）。"""
    res = requests.post(
        config.PUSH_URL,
        headers={
            'Authorization': f'Bearer {config.CHANNEL_ACCESS_TOKEN}',
            'Content-Type': 'application/json',
        },
        data=json.dumps({'to': to, 'messages': _messages(text)}),
        timeout=20,
    )
    if res.status_code >= 300:
        print('LINE push error', res.status_code, res.text)
        return False
    return True
