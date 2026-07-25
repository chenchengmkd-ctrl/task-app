"""GitHubへの軽微なハートビートコミット。週次レポート送信時に呼ばれる。
GitHub Actionsの「60日間コミットが無いと定期実行が自動停止する」仕様を回避するためのもの。
"""
import base64

import requests

from . import config


def push_heartbeat_commit():
    """line-bot-vercel/HEARTBEAT.md を今日の日付で更新するコミットを1つ打つ。失敗しても致命的ではない。"""
    if not config.GITHUB_PAT or not config.GITHUB_REPO:
        return
    path = 'line-bot-vercel/HEARTBEAT.md'
    url = f'https://api.github.com/repos/{config.GITHUB_REPO}/contents/{path}'
    headers = {
        'Authorization': f'Bearer {config.GITHUB_PAT}',
        'Accept': 'application/vnd.github+json',
    }
    sha = None
    try:
        res = requests.get(url, headers=headers, timeout=15)
        if res.status_code == 200:
            sha = res.json().get('sha')
    except requests.RequestException as e:
        print('heartbeat get error:', e)
        return

    content_text = f'最終確認: {config.today_iso()}（GitHub Actionsの60日自動停止を防ぐための自動コミットです）\n'
    body = {
        'message': f'chore: heartbeat {config.today_iso()}',
        'content': base64.b64encode(content_text.encode('utf-8')).decode('ascii'),
        'branch': 'main',
    }
    if sha:
        body['sha'] = sha
    try:
        requests.put(url, headers=headers, json=body, timeout=15)
    except requests.RequestException as e:
        print('heartbeat put error:', e)
