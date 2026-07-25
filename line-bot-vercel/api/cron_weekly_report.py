"""毎週月曜9:00（JST）：週次レポート。加えてGitHubに軽微なハートビートコミットを打ち、
GitHub Actionsの「60日間コミットが無いと定期実行が自動停止する」仕様を回避する。
"""
import base64
from http.server import BaseHTTPRequestHandler

import requests

from linebot import config
from linebot.agent import send_weekly_report
from linebot.line_client import push_text, get_users


def _push_heartbeat_commit():
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


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        auth = self.headers.get('Authorization', '')
        if not config.CRON_SECRET or auth != f'Bearer {config.CRON_SECRET}':
            self.send_response(401)
            self.end_headers()
            return

        try:
            send_weekly_report(push_text, get_users)
        except Exception as e:
            print('send_weekly_report error:', e)

        try:
            _push_heartbeat_commit()
        except Exception as e:
            print('heartbeat error:', e)

        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(b'{"ok": true}')
