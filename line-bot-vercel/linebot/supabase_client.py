"""Supabase (PostgREST) ヘルパー。Code.gs の getSupabase/postSupabase/patchSupabase に相当。
bot_state テーブルは Apps Script の PropertiesService（保留返信の状態管理など）の置き換え。
"""
import json
from urllib.parse import quote

import requests

from . import config


def _headers(prefer=None):
    h = {
        'apikey': config.SUPABASE_ANON_KEY,
        'Authorization': f'Bearer {config.SUPABASE_ANON_KEY}',
    }
    if prefer:
        h['Prefer'] = prefer
    return h


def get_supabase(table, params):
    url = f'{config.SUPABASE_URL}/rest/v1/{table}?{params}'
    res = requests.get(url, headers={**_headers(), 'Accept': 'application/json'}, timeout=20)
    if res.status_code != 200:
        print('Supabase error', res.status_code, res.text)
        return []
    return res.json()


def post_supabase(table, rows):
    url = f'{config.SUPABASE_URL}/rest/v1/{table}'
    res = requests.post(
        url,
        headers={**_headers('return=minimal'), 'Content-Type': 'application/json'},
        data=json.dumps(rows),
        timeout=20,
    )
    if res.status_code >= 300:
        print('Supabase insert error', res.status_code, res.text)
        return False
    return True


def patch_supabase(table, filter_query, body):
    url = f'{config.SUPABASE_URL}/rest/v1/{table}?{filter_query}'
    res = requests.patch(
        url,
        headers={**_headers('return=representation'), 'Content-Type': 'application/json'},
        data=json.dumps(body),
        timeout=20,
    )
    if res.status_code >= 300:
        print('Supabase update error', res.status_code, res.text)
        return None
    try:
        return res.json()
    except ValueError:
        return None


def delete_supabase(table, filter_query):
    """行を物理削除する（tasksのような論理削除=deletedフラグではなく、recurringのように削除列を持たないテーブル用）。"""
    url = f'{config.SUPABASE_URL}/rest/v1/{table}?{filter_query}'
    res = requests.delete(url, headers=_headers(), timeout=20)
    if res.status_code >= 300:
        print('Supabase delete error', res.status_code, res.text)
        return False
    return True


# ============ bot_state（key/valueストア。PropertiesServiceの置き換え） ============
def get_state(key):
    """保存されている値（dict/list等、JSON化されたもの）を返す。無ければNone。"""
    rows = get_supabase('bot_state', f'key=eq.{quote(key)}&select=value')
    if not rows:
        return None
    return rows[0].get('value')


def set_state(key, value):
    """key/valueを保存（既にあれば上書き）。"""
    url = f'{config.SUPABASE_URL}/rest/v1/bot_state?on_conflict=key'
    res = requests.post(
        url,
        headers={**_headers('resolution=merge-duplicates,return=minimal'), 'Content-Type': 'application/json'},
        data=json.dumps([{'key': key, 'value': value}]),
        timeout=20,
    )
    return res.status_code < 300


def delete_state(key):
    """key/valueを削除。"""
    url = f'{config.SUPABASE_URL}/rest/v1/bot_state?key=eq.{quote(key)}'
    res = requests.delete(url, headers=_headers(), timeout=20)
    return res.status_code < 300


def list_state_keys(prefix):
    """指定した接頭辞から始まるキー一覧を返す（古い状態のクリーンアップ用）。"""
    rows = get_supabase('bot_state', f'key=like.{quote(prefix)}*&select=key')
    return [r['key'] for r in rows]
