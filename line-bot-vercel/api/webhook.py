"""LINEのWebhook受信エンドポイント（POST）。Code.gs の doPost/handleEvent に相当。"""
import json
from http.server import BaseHTTPRequestHandler

from linebot.router import handle_event


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get('Content-Length', 0) or 0)
        raw_body = self.rfile.read(length) if length else b'{}'

        try:
            body = json.loads(raw_body.decode('utf-8'))
            for ev in body.get('events', []):
                try:
                    handle_event(ev)
                except Exception as e:  # 1件の処理失敗で他のイベントを止めない
                    print('handle_event error:', e)
        except Exception as e:
            print('webhook error:', e)

        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps({'ok': True}).encode('utf-8'))
