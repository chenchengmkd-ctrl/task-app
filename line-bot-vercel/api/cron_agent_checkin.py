"""毎晩20:00（JST）：AIコーチの進捗チェックイン。Vercel Cronから呼ばれる（GET）。"""
from http.server import BaseHTTPRequestHandler

from linebot import config
from linebot.agent import send_agent_checkin
from linebot.line_client import push_text, get_users


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        auth = self.headers.get('Authorization', '')
        if not config.CRON_SECRET or auth != f'Bearer {config.CRON_SECRET}':
            self.send_response(401)
            self.end_headers()
            return

        try:
            send_agent_checkin(push_text, get_users)
        except Exception as e:
            print('send_agent_checkin error:', e)

        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(b'{"ok": true}')
