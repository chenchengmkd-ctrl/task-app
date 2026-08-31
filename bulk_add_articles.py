#!/usr/bin/env python3
"""article_urls.txt のURLを1件ずつ「思考トレース」アプリに取り込む一括投入スクリプト。

study.html の「記事・ブログ（URL）」ボタンと同じ処理を再現する:
  1) Supabase REST に mentor_sources 行を作る（kind="article"）
  2) POST /api/mentor_ingest {phase:"start"}   … サーバーが記事本文を取得
  3) POST /api/mentor_ingest {phase:"distill"} … Geminiが要約・逐語引用を抽出

使い方:
  python bulk_add_articles.py --list-mentors
  python bulk_add_articles.py --mentor "小野内さん"
  python bulk_add_articles.py article_urls.txt --mentor-id abc123 --interval 6

- URLファイルは1行1URL（空行と # で始まる行は無視）。
- 既に同じ人物に同じURLが取り込み済み(status=done)ならスキップ。
- 失敗は article_ingest_failures.log に、全経過は article_ingest.log に記録。
- リクエスト間隔（既定5秒）は --interval で変更。
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# study.html と同じ公開値（環境変数で上書き可）
SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://xldkfkhgazpugfuscpqt.supabase.co")
SUPABASE_ANON_KEY = os.environ.get(
    "SUPABASE_ANON_KEY",
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InhsZGtma2hnYXpwdWdmdXNjcHF0Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODM4NDg3MzgsImV4cCI6MjA5OTQyNDczOH0.C3_TYQI8R3HeXYWTzca9erUMjpTWm2sneB7hk5Bre8Y",
)
API_BASE = os.environ.get("MENTOR_API_BASE", "https://task-app-nine-liard.vercel.app")
APP_TOKEN = os.environ.get("MENTOR_APP_TOKEN", "")  # Vercelで設定している場合のみ

LOG_FILE = "article_ingest.log"
FAIL_FILE = "article_ingest_failures.log"
INGEST_TIMEOUT = 150  # 記事取得＋Gemini要約で数十秒かかることがある


def _now():
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def log(msg, *, fail=False):
    line = f"[{_now()}] {msg}"
    print(line)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    if fail:
        with open(FAIL_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def _request(url, *, method="GET", body=None, headers=None, timeout=30):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"raw": raw[:500]}
    except Exception as e:  # noqa: BLE001  (通信断・タイムアウト等はまとめて拾う)
        return 0, {"exception": repr(e)}


# ---------- Supabase ----------
_SB_HEADERS = {"apikey": SUPABASE_ANON_KEY, "Authorization": f"Bearer {SUPABASE_ANON_KEY}"}


def sb_get(path):
    return _request(f"{SUPABASE_URL}/rest/v1/{path}", headers=_SB_HEADERS)


def sb_insert(table, row):
    return _request(
        f"{SUPABASE_URL}/rest/v1/{table}",
        method="POST", body=[row],
        headers={**_SB_HEADERS, "Prefer": "return=minimal"},
    )


def list_mentors():
    status, data = sb_get("mentors?select=id,name&order=created_at")
    if status != 200:
        log(f"人物一覧の取得に失敗 (HTTP {status}): {data}", fail=True)
        sys.exit(1)
    return data


def resolve_mentor(args):
    mentors = list_mentors()
    if args.list_mentors:
        print("\n登録済みの人物:")
        for m in mentors:
            print(f"  {m['id']}  {m['name']}")
        sys.exit(0)
    if args.mentor_id:
        hit = [m for m in mentors if m["id"] == args.mentor_id]
        if not hit:
            log(f"mentor-id が見つかりません: {args.mentor_id}", fail=True)
            sys.exit(1)
        return hit[0]
    if args.mentor:
        exact = [m for m in mentors if m["name"] == args.mentor]
        part = [m for m in mentors if args.mentor in m["name"]]
        hit = exact or part
        if len(hit) != 1:
            log(f"人物名 '{args.mentor}' が一意に定まりません（候補 {len(hit)} 件）。--list-mentors で確認してください。", fail=True)
            sys.exit(1)
        return hit[0]
    log("対象の人物を指定してください（--mentor \"名前\" または --mentor-id ID）。--list-mentors で一覧表示。", fail=True)
    sys.exit(1)


def existing_source(mentor_id, url):
    q = f"mentor_sources?mentor_id=eq.{urllib.parse.quote(mentor_id)}&url=eq.{urllib.parse.quote(url)}&select=id,status"
    status, data = sb_get(q)
    return data[0] if status == 200 and data else None


# ---------- 取り込みAPI ----------
def _api_headers():
    h = {}
    if APP_TOKEN:
        h["X-App-Token"] = APP_TOKEN
    return h


def ingest_phase(source_id, phase):
    return _request(
        f"{API_BASE}/api/mentor_ingest",
        method="POST", body={"source_id": source_id, "phase": phase},
        headers=_api_headers(), timeout=INGEST_TIMEOUT,
    )


def new_id():
    return format(int(time.time() * 1000), "x") + os.urandom(3).hex()


def add_one(mentor_id, url):
    """1件を取り込む。戻り値: 'done' / 'skip' / 'fail'"""
    prior = existing_source(mentor_id, url)
    if prior and prior.get("status") == "done":
        log(f"SKIP  取り込み済み: {url}")
        return "skip"

    source_id = prior["id"] if prior else new_id()
    if not prior:
        st, resp = sb_insert("mentor_sources", {
            "id": source_id, "mentor_id": mentor_id, "kind": "article",
            "url": url, "title": url, "status": "processing",
        })
        if st >= 300:
            log(f"FAIL  行の作成に失敗 (HTTP {st}) {url} :: {resp}", fail=True)
            return "fail"

    # phase: start（サーバーが記事本文を取得）
    st, resp = ingest_phase(source_id, "start")
    if st != 200 or resp.get("phase") == "error":
        log(f"FAIL  start {url} (HTTP {st}) :: {resp.get('message') or resp}", fail=True)
        return "fail"
    if resp.get("phase") != "distill":
        log(f"FAIL  start 想定外の応答 {url} :: {resp}", fail=True)
        return "fail"

    # phase: distill（Geminiが要約・逐語引用を抽出）
    st, resp = ingest_phase(source_id, "distill")
    if st != 200 or resp.get("phase") == "error":
        log(f"FAIL  distill {url} (HTTP {st}) :: {resp.get('message') or resp}", fail=True)
        return "fail"
    if resp.get("phase") != "done":
        log(f"FAIL  distill 想定外の応答 {url} :: {resp}", fail=True)
        return "fail"

    log(f"OK    {url}  → 「{resp.get('title', '')}」  引用{len(resp.get('quotes') or [])}件")
    return "done"


def read_urls(path):
    if not os.path.exists(path):
        log(f"URLファイルが見つかりません: {path}", fail=True)
        sys.exit(1)
    seen, urls = set(), []
    with open(path, encoding="utf-8") as f:
        for line in f:
            u = line.strip()
            if not u or u.startswith("#"):
                continue
            if not u.lower().startswith(("http://", "https://")):
                log(f"URLでない行をスキップ: {u}")
                continue
            if u not in seen:
                seen.add(u)
                urls.append(u)
    return urls


def main():
    ap = argparse.ArgumentParser(description="記事URLを思考トレースアプリに一括取り込み")
    ap.add_argument("urls_file", nargs="?", default="article_urls.txt", help="1行1URLのテキストファイル（既定: article_urls.txt）")
    ap.add_argument("--mentor", help="対象の人物名（部分一致可）")
    ap.add_argument("--mentor-id", dest="mentor_id", help="対象の人物ID")
    ap.add_argument("--list-mentors", action="store_true", help="登録済みの人物一覧を表示して終了")
    ap.add_argument("--interval", type=float, default=5.0, help="1件ごとの待ち時間（秒、既定5）")
    args = ap.parse_args()

    mentor = resolve_mentor(args)
    urls = read_urls(args.urls_file)
    log(f"=== 開始: 人物「{mentor['name']}」({mentor['id']}) / {len(urls)}件 / 間隔{args.interval}秒 ===")

    tally = {"done": 0, "skip": 0, "fail": 0}
    for i, url in enumerate(urls, 1):
        log(f"[{i}/{len(urls)}] {url}")
        try:
            tally[add_one(mentor["id"], url)] += 1
        except Exception as e:  # noqa: BLE001
            log(f"FAIL  例外 {url} :: {e!r}", fail=True)
            tally["fail"] += 1
        if i < len(urls):
            time.sleep(args.interval)

    log(f"=== 完了: 成功{tally['done']} / スキップ{tally['skip']} / 失敗{tally['fail']} ===")
    if tally["fail"]:
        log(f"失敗の詳細は {FAIL_FILE} を参照。プロファイルは study.html の「思考プロファイル」→「再生成」で更新してください。")
    sys.exit(1 if tally["fail"] else 0)


if __name__ == "__main__":
    main()
