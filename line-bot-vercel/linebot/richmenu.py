"""LINEリッチメニュー（トーク画面下部のメニュー）の作成・画像アップロード・既定メニュー設定。

画像はブラウザのCanvasで生成したものを /api/setup_richmenu にPOSTして渡す。
Vercelのサーバーレス環境には永続的なファイル保存が無いため、画像はリポジトリに置かず、
その場でLINE側にアップロードして完結させる。
"""
import base64
import json

import requests

from . import config

APP_URL = 'https://chenchengmkd-ctrl.github.io/task-app/'

# 1200x810の画像を 3列×3行（各 400x270）に分割したときの各ボタン。
# 並びは画像（Canvasで描く CELLS）と必ず一致させること。
AREAS = [
    (0, 0, '今日の予定'),
    (400, 0, '明日の予定'),
    (800, 0, '一覧'),
    (0, 270, '通知'),
    (400, 270, '資料'),
    (800, 270, '振り返り'),
    (0, 540, '相談'),
    (400, 540, None),      # アプリ（URLを開く）
    (800, 540, 'ヘルプ'),
]
CELL_W, CELL_H = 400, 270


def _auth_headers():
    return {'Authorization': f'Bearer {config.CHANNEL_ACCESS_TOKEN}'}


def build_menu_definition():
    areas = []
    for x, y, message in AREAS:
        if message is None:
            action = {'type': 'uri', 'label': 'アプリ', 'uri': APP_URL}
        else:
            action = {'type': 'message', 'label': message, 'text': message}
        areas.append({
            'bounds': {'x': x, 'y': y, 'width': CELL_W, 'height': CELL_H},
            'action': action,
        })
    return {
        'size': {'width': 1200, 'height': 810},
        'selected': True,
        'name': 'My Tasks main menu',
        'chatBarText': 'メニュー',
        'areas': areas,
    }


def delete_existing_menus():
    """既に登録されているリッチメニューを全部消す（作り直しのたびに増えないように）。"""
    deleted = []
    try:
        res = requests.get('https://api.line.me/v2/bot/richmenu/list', headers=_auth_headers(), timeout=20)
        if res.status_code != 200:
            return {'error': f'list failed {res.status_code}', 'body': res.text[:300]}
        for m in res.json().get('richmenus', []):
            rid = m.get('richMenuId')
            d = requests.delete(f'https://api.line.me/v2/bot/richmenu/{rid}', headers=_auth_headers(), timeout=20)
            deleted.append({'id': rid, 'status': d.status_code})
    except requests.RequestException as e:
        return {'error': str(e)}
    return deleted


def setup_rich_menu(image_data_url):
    """リッチメニューを作成し、画像をアップロードして、全ユーザーの既定メニューに設定する。"""
    if not config.CHANNEL_ACCESS_TOKEN:
        return {'ok': False, 'error': 'CHANNEL_ACCESS_TOKEN not set'}
    if not image_data_url or ',' not in image_data_url:
        return {'ok': False, 'error': 'image data url missing or malformed'}

    header, b64 = image_data_url.split(',', 1)
    content_type = 'image/png' if 'png' in header else 'image/jpeg'
    try:
        image_bytes = base64.b64decode(b64)
    except Exception as e:
        return {'ok': False, 'error': f'base64 decode failed: {e}'}

    result = {'ok': True, 'image_bytes': len(image_bytes), 'content_type': content_type}
    result['deleted_previous'] = delete_existing_menus()

    # 1. メニュー定義を作成
    res = requests.post(
        'https://api.line.me/v2/bot/richmenu',
        headers={**_auth_headers(), 'Content-Type': 'application/json'},
        data=json.dumps(build_menu_definition()).encode('utf-8'),
        timeout=20,
    )
    if res.status_code != 200:
        return {'ok': False, 'step': 'create', 'status': res.status_code, 'body': res.text[:500]}
    rich_menu_id = res.json().get('richMenuId')
    result['richMenuId'] = rich_menu_id

    # 2. 画像をアップロード（アップロードだけホストが api-data.line.me である点に注意）
    res = requests.post(
        f'https://api-data.line.me/v2/bot/richmenu/{rich_menu_id}/content',
        headers={**_auth_headers(), 'Content-Type': content_type},
        data=image_bytes,
        timeout=40,
    )
    if res.status_code != 200:
        return {'ok': False, 'step': 'upload', 'status': res.status_code, 'body': res.text[:500],
                'richMenuId': rich_menu_id}

    # 3. 全ユーザーの既定メニューにする
    res = requests.post(
        f'https://api.line.me/v2/bot/user/all/richmenu/{rich_menu_id}',
        headers=_auth_headers(),
        timeout=20,
    )
    if res.status_code != 200:
        return {'ok': False, 'step': 'set_default', 'status': res.status_code, 'body': res.text[:500],
                'richMenuId': rich_menu_id}

    return result
