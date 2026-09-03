"""思考トレースアプリ(Supabase)から、ある人物のソースをローカルに書き出す。
サブエージェント（.claude/agents/*.md）が読むための素材づくり。

  python study-data/export_mentor.py "黒猫アイランド" kuroneko-island

- study-data/<slug>/articles.md      … タイトル+要約+逐語引用（コミットする。軽い・transformative）
- study-data/<slug>/transcripts/*.md  … 全文（.gitignore対象。重い・元記事の丸写しに近いため）
"""
import json
import os
import re
import sys
import urllib.parse
import urllib.request

SB = "https://xldkfkhgazpugfuscpqt.supabase.co/rest/v1"
KEY = os.environ.get("SUPABASE_ANON_KEY", (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InhsZGtma2hnYXpwdWdmdXNjcHF0Iiwicm9sZSI6"
    "ImFub24iLCJpYXQiOjE3ODM4NDg3MzgsImV4cCI6MjA5OTQyNDczOH0.C3_TYQI8R3HeXYWTzca9erUMjpTWm2sneB7hk5Bre8Y"))
H = {"apikey": KEY, "Authorization": f"Bearer {KEY}"}
HERE = os.path.dirname(os.path.abspath(__file__))


def get(path):
    return json.loads(urllib.request.urlopen(urllib.request.Request(f"{SB}/{path}", headers=H), timeout=60).read().decode())


def main():
    if len(sys.argv) < 3:
        sys.exit('usage: python export_mentor.py "<人物名>" <slug>')
    name, slug = sys.argv[1], sys.argv[2]
    mentors = get(f"mentors?name=eq.{urllib.parse.quote(name)}&select=id,name,profile")
    if not mentors:
        sys.exit(f'人物「{name}」が見つかりません')
    m = mentors[0]

    rows = []
    step = 200
    for off in range(0, 5000, step):
        page = get(f"mentor_sources?mentor_id=eq.{m['id']}&status=eq.done"
                   f"&select=url,title,summary,quotes,transcript,created_at"
                   f"&order=created_at.asc&limit={step}&offset={off}")
        rows += page
        if len(page) < step:
            break

    out_dir = os.path.join(HERE, slug)
    tr_dir = os.path.join(out_dir, "transcripts")
    os.makedirs(tr_dir, exist_ok=True)

    def entry_no(url):
        mm = re.search(r'entry-(\d+)', url or '')
        return mm.group(1) if mm else re.sub(r'\W+', '_', (url or 'x'))[-12:]

    with open(os.path.join(out_dir, "articles.md"), "w", encoding="utf-8") as f:
        f.write(f"# {m['name']} — 記事の要約と逐語引用（{len(rows)}本）\n\n")
        f.write("各記事: タイトル / 要約 / この人の考えがそのまま出ている発言。"
                "全文が要るときは transcripts/<番号>.md を読む。\n\n")
        for r in rows:
            no = entry_no(r["url"])
            f.write(f"\n## [{no}] {r.get('title') or '（無題）'}\n")
            f.write(f"- URL: {r['url']}\n")
            f.write(f"- 要約: {r.get('summary') or ''}\n")
            qs = r.get("quotes") or []
            if qs:
                f.write("- 逐語引用:\n")
                for q in qs:
                    c = f"（{q.get('context')}）" if q.get("context") else ""
                    f.write(f"  - 「{q.get('quote', '')}」{c}\n")

    for r in rows:
        no = entry_no(r["url"])
        with open(os.path.join(tr_dir, f"{no}.md"), "w", encoding="utf-8") as f:
            f.write(f"# [{no}] {r.get('title') or ''}\n{r['url']}\n\n{r.get('transcript') or ''}\n")

    total_chars = sum(len(r.get("transcript") or "") for r in rows)
    print(f"{m['name']}: {len(rows)}本 書き出し / 全文合計 {total_chars:,}字")
    print(f"  {out_dir}/articles.md")
    print(f"  {tr_dir}/*.md ({len(rows)}ファイル)")


if __name__ == "__main__":
    main()
