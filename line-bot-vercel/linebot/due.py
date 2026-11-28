"""会社のお金の期日（請求書の発行日・支払い期日・契約の更新日・税金の納付期限）を見張る（定期報告 teiki.html の「期日」タブ用）。

- 期日は bot_state の due:items に置く。スマホとiPadで同時に触っても消し合わないよう、1件ずつ書き換える
- 通知は Web Push（ホーム画面に追加したページへ届く。アプリを閉じていても届く）。
  LINEは月の無料通数を使い切ったことがあるため使わない
- 5分おきのポーリング（/api/cron_poll）から notify_step が呼ばれ、朝9時台に1回だけ
  「明日が期日」「今日が期日」を送る
- 送信に使う鍵（VAPID）は VAPID_PRIVATE_KEY があればそれ、なければ POLL_SECRET から毎回同じものを作る
  （どこにも保存しない。POLL_SECRET を変えたら、ページで通知をオンにし直す）
"""
import base64
import hashlib
import hmac
import json
import os
import struct
import time
import uuid
from datetime import timedelta
from urllib.parse import urlparse

import requests

from . import config
from .supabase_client import get_state, set_state

ITEMS_KEY = 'due:items'
SUBS_KEY = 'due:subs'
SENT_KEY = 'due:sent:{}'
NOTIFY_AT = (9, 0)
NOTIFY_WINDOW_MIN = 60          # ポーリングが遅れても9時台なら送る。10時を過ぎたらその日は送らない
KINDS = {'invoice': '請求書の発行', 'pay': '支払い', 'renew': '契約の更新', 'tax': '税金の納付'}
MAX_ITEMS = 500
MAX_SUBS = 10
APP_URL = 'https://chenchengmkd-ctrl.github.io/task-app/teiki.html#kg'
VAPID_SUB = 'https://chenchengmkd-ctrl.github.io/task-app/'


# ============ 期日の読み書き ============
def _items():
    v = get_state(ITEMS_KEY)
    return v if isinstance(v, list) else []


def _clean(it):
    kind = str(it.get('kind') or '')
    if kind not in KINDS:
        raise ValueError('種類が違います')
    name = str(it.get('name') or '').strip()[:80]
    if not name:
        raise ValueError('名前を入れてください')
    d = str(it.get('date') or '')
    if not config.parse_date(d) or len(d) != 10:
        raise ValueError('日付の形が違います')
    amt = it.get('amount')
    if amt in (None, ''):
        amt = None
    else:
        try:
            amt = int(round(float(str(amt).replace(',', ''))))
        except ValueError:
            raise ValueError('金額は数字で入れてください')
        if abs(amt) > 10 ** 12:
            raise ValueError('金額が大きすぎます')
    return {'kind': kind, 'name': name, 'amount': amt, 'date': d, 'memo': str(it.get('memo') or '').strip()[:200]}


def apply(op, body):
    """1件ずつの書き換え。→ 書き換え後の一覧"""
    items = _items()
    now = config.now_jst().isoformat(timespec='seconds')
    iid = str(body.get('id') or '')
    if op == 'save':
        fields = _clean(body.get('item') or {})
        for it in items:
            if it.get('id') == iid:
                it.update(fields, u=now)
                break
        else:
            if len(items) >= MAX_ITEMS:
                raise ValueError('登録できる数の上限です。済みのものを消してください')
            items.append({'id': uuid.uuid4().hex[:12], **fields, 'done': None, 'u': now})
    elif op in ('done', 'undo', 'delete'):
        hit = [it for it in items if it.get('id') == iid]
        if not hit:
            raise ValueError('見つかりません（ほかの端末で消された可能性があります）')
        if op == 'delete':
            items = [it for it in items if it.get('id') != iid]
        else:
            hit[0].update(done=now if op == 'done' else None, u=now)
    else:
        raise ValueError('op が違います')
    set_state(ITEMS_KEY, items)
    return items


def list_items():
    return _items()


# ============ Web Push ============
def _b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b'=').decode()


def _b64u_dec(s):
    s = str(s)
    return base64.urlsafe_b64decode(s + '=' * (-len(s) % 4))


def _vapid_key():
    from cryptography.hazmat.primitives.asymmetric import ec
    raw = os.environ.get('VAPID_PRIVATE_KEY', '').strip()
    if raw:
        n = int.from_bytes(_b64u_dec(raw), 'big')
    else:
        seed = config.POLL_SECRET or config.CRON_SECRET or config.CHANNEL_ACCESS_TOKEN
        if not seed:
            raise RuntimeError('通知の鍵を作れません（POLL_SECRET が未設定）')
        order = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
        n = int.from_bytes(hashlib.sha256(('task-app-vapid:' + seed).encode()).digest(), 'big') % (order - 1) + 1
    return ec.derive_private_key(n, ec.SECP256R1())


def _pub_bytes(key):
    from cryptography.hazmat.primitives import serialization
    return key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def public_key():
    return _b64u(_pub_bytes(_vapid_key()))


def _vapid_header(endpoint, key):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
    u = urlparse(endpoint)
    head = _b64u(json.dumps({'typ': 'JWT', 'alg': 'ES256'}, separators=(',', ':')).encode())
    claims = _b64u(json.dumps({'aud': f'{u.scheme}://{u.netloc}', 'exp': int(time.time()) + 12 * 3600,
                               'sub': VAPID_SUB}, separators=(',', ':')).encode())
    r, s = decode_dss_signature(key.sign(f'{head}.{claims}'.encode(), ec.ECDSA(hashes.SHA256())))
    jwt = f'{head}.{claims}.{_b64u(r.to_bytes(32, "big") + s.to_bytes(32, "big"))}'
    return f'vapid t={jwt}, k={_b64u(_pub_bytes(key))}'


def _hkdf(salt, ikm, info, length):
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    return hmac.new(prk, info + b'\x01', hashlib.sha256).digest()[:length]


def encrypt(payload, p256dh, auth):
    """RFC 8291（aes128gcm）でブラウザ宛ての中身を暗号化する。"""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    ua_pub = _b64u_dec(p256dh)
    auth_secret = _b64u_dec(auth)
    eph = ec.generate_private_key(ec.SECP256R1())
    as_pub = eph.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    shared = eph.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_pub))
    ikm = _hkdf(auth_secret, shared, b'WebPush: info\x00' + ua_pub + as_pub, 32)
    salt = os.urandom(16)
    cek = _hkdf(salt, ikm, b'Content-Encoding: aes128gcm\x00', 16)
    nonce = _hkdf(salt, ikm, b'Content-Encoding: nonce\x00', 12)
    body = AESGCM(cek).encrypt(nonce, payload + b'\x02', None)
    return salt + struct.pack('!I', 4096) + bytes([len(as_pub)]) + as_pub + body


def _send(sub, data, key):
    """→ 'ok' / 'gone'（もう届かない購読）/ 'error'"""
    try:
        body = encrypt(json.dumps(data, ensure_ascii=False).encode(), sub['keys']['p256dh'], sub['keys']['auth'])
        res = requests.post(sub['endpoint'], data=body, timeout=15, headers={
            'Authorization': _vapid_header(sub['endpoint'], key), 'Content-Encoding': 'aes128gcm',
            'Content-Type': 'application/octet-stream', 'TTL': str(20 * 3600), 'Urgency': 'high'})
    except Exception as e:
        print('due push error', repr(e)[:200])
        return 'error'
    if res.status_code in (404, 410):
        return 'gone'
    if res.status_code >= 300:
        print('due push status', res.status_code, res.text[:200])
        return 'error'
    return 'ok'


def push_all(messages):
    """messages: [{title, body, tag}] を登録済みの端末すべてへ。→ 届けた件数"""
    subs = get_state(SUBS_KEY) or []
    if not subs or not messages:
        return 0
    key = _vapid_key()
    gone, sent = set(), 0
    for sub in subs:
        for m in messages:
            r = _send(sub, {**m, 'url': APP_URL}, key)
            if r == 'gone':
                gone.add(sub['endpoint'])
                break
            sent += r == 'ok'
    if gone:
        set_state(SUBS_KEY, [s for s in subs if s['endpoint'] not in gone])
    return sent


def subscribe(sub, label=''):
    if not (isinstance(sub, dict) and str(sub.get('endpoint', '')).startswith('https://')
            and isinstance(sub.get('keys'), dict) and sub['keys'].get('p256dh') and sub['keys'].get('auth')):
        raise ValueError('通知の登録情報が読めません')
    subs = [s for s in (get_state(SUBS_KEY) or []) if s.get('endpoint') != sub['endpoint']]
    subs.append({'endpoint': sub['endpoint'], 'keys': {'p256dh': sub['keys']['p256dh'], 'auth': sub['keys']['auth']},
                 'label': str(label)[:40], 'at': config.now_jst().isoformat(timespec='seconds')})
    set_state(SUBS_KEY, subs[-MAX_SUBS:])
    return len(subs[-MAX_SUBS:])


def unsubscribe(endpoint):
    subs = get_state(SUBS_KEY) or []
    left = [s for s in subs if s.get('endpoint') != endpoint]
    set_state(SUBS_KEY, left)
    return len(left)


def sub_count():
    return len(get_state(SUBS_KEY) or [])


# ============ 朝9時のお知らせ ============
def _yen(v):
    return '' if v is None else f'　{v:,}円'


def messages_for(today_iso, items):
    tomorrow = config.iso_of_date(config.parse_date(today_iso) + timedelta(days=1))
    out = []
    for when, d in (('今日', today_iso), ('明日', tomorrow)):
        for it in sorted((x for x in items if not x.get('done') and x.get('date') == d), key=lambda x: x.get('name', '')):
            out.append({'title': f'{when}が{KINDS.get(it["kind"], "期日")}の期日です',
                        'body': f'{it["name"]}{_yen(it.get("amount"))}', 'tag': f'due-{it["id"]}-{d}-{when}'})
    return out


def notify_step(push_text_fn=None, get_users_fn=None, now=None):
    """5分おきに呼ばれる。9時台に1回だけ、明日と今日が期日のものを送る。"""
    now = now or config.now_jst()
    target = now.replace(hour=NOTIFY_AT[0], minute=NOTIFY_AT[1], second=0, microsecond=0)
    if not (0 <= (now - target).total_seconds() / 60 < NOTIFY_WINDOW_MIN):
        return
    today = config.iso_of_date(now)
    sent_key = SENT_KEY.format(today)
    already = set(get_state(sent_key) or [])
    msgs = [m for m in messages_for(today, _items()) if m['tag'] not in already]
    if not msgs:
        return
    # 先に「送った」と記録してから送る（ポーリングが重なっても二重に鳴らさない）
    set_state(sent_key, sorted(already | {m['tag'] for m in msgs}))
    push_all(msgs)
