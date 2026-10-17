# -*- coding: utf-8 -*-
"""flyer-data.js の内容から版番号を作り、flyer.html の読み込み行に書き込む。

flyer-data.js を作り直したら、このスクリプトを1回実行する。
  python tools/bump_flyer_data_version.py

版番号は内容のハッシュなので、データが変わらなければ番号も変わらない
(ブラウザのキャッシュがそのまま使われる)。変わったときだけ新しいデータを取りに行く。
"""
import hashlib
import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, 'flyer-data.js')
HTML = os.path.join(ROOT, 'flyer.html')

data = io.open(DATA, encoding='utf-8', newline='').read()
ver = hashlib.md5(data.encode('utf-8')).hexdigest()[:8]

html = io.open(HTML, encoding='utf-8', newline='').read()
new, n = re.subn(r'(<script src="flyer-data\.js)\?v=[0-9a-f]+(")', r'\1?v=' + ver + r'\2', html)
if n != 1:
    sys.exit('flyer.html に flyer-data.js の読み込み行が1つだけ見つかりません（%d個）' % n)
if new == html:
    print('版番号は変わりません:', ver)
else:
    io.open(HTML, 'w', encoding='utf-8', newline='').write(new)
    print('版番号を更新しました:', ver)
