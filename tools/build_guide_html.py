"""ガイドブックのMarkdownからHTMLを作る（タイトルはMarkdownの最初の「# 見出し」）。
使い方: pip install markdown && python3 tools/build_guide_html.py NIPPO_GUIDE.md nippo-guide.html
短いガイドは --compact を付ける（印刷で章ごとに改ページしない）
PDFは続けて: node tools/guide_pdf.js nippo-guide.html NIPPO_GUIDE.pdf（Playwrightが必要）
"""
import re
import sys

import markdown

args = [a for a in sys.argv[1:] if not a.startswith('--')]
md_path, html_path = args[0], args[1]
# --compact: 1〜2ページの短いガイド用。印刷で章ごとに改ページせず、余白・文字を詰める
COMPACT = '--compact' in sys.argv


def gh_slug(value, separator='-'):
    # GitHubと同じ見出しアンカー（目次リンクをそのまま使うため）
    v = value.strip().lower()
    v = re.sub(r'[^\w\- ]', '', v)
    return v.replace(' ', separator)


src = open(md_path, encoding='utf-8').read()
m = re.search(r'^# (.+)$', src, re.M)
md_name = __import__('os').path.basename(md_path)
title = m.group(1).strip() if m else 'ガイドブック'
body = markdown.markdown(
    src,
    extensions=['tables', 'fenced_code', 'toc'],
    extension_configs={'toc': {'slugify': gh_slug}},
)
body = body.replace('<table>', '<div class="scroll"><table>').replace('</table>', '</table></div>')
# 外部リンクは新しいタブで
body = re.sub(r'<a href="(https?://[^"]+)">', r'<a href="\1" target="_blank" rel="noopener">', body)

CSS = r"""
:root{--bg:#f8f4ef;--card:#fff;--ink:#1f1d1a;--muted:#7b766f;--line:#ebe5dd;--soft:#fbf3ec;
  --accent:#e8552d;--accent-ink:#c2410c;--code-bg:#f4efe9;--quote:#fff4ea}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){
  --bg:#141210;--card:#1d1a17;--ink:#ece6df;--muted:#a39a90;--line:#332e29;--soft:#26211c;
  --accent:#ff8a5c;--accent-ink:#ffa27e;--code-bg:#24201c;--quote:#2a221b}}
:root[data-theme="dark"]{--bg:#141210;--card:#1d1a17;--ink:#ece6df;--muted:#a39a90;--line:#332e29;--soft:#26211c;
  --accent:#ff8a5c;--accent-ink:#ffa27e;--code-bg:#24201c;--quote:#2a221b}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
body{margin:0;background:var(--bg);color:var(--ink);
  font-family:-apple-system,BlinkMacSystemFont,"Hiragino Sans","Hiragino Kaku Gothic ProN","Yu Gothic UI","Meiryo","IPAPGothic","IPAGothic",sans-serif;
  font-size:16px;line-height:1.8;-webkit-text-size-adjust:100%}
.top{background:linear-gradient(135deg,#ff9a3c 0%,#ff5e62 100%);color:#fff;padding:28px 16px 22px}
.top .in{max-width:820px;margin:0 auto}
.top .k{font-size:13px;font-weight:700;letter-spacing:.08em;opacity:.9}
.top h1{margin:4px 0 0;font-size:28px;line-height:1.3}
main{max-width:820px;margin:0 auto;padding:8px 16px 80px}
main>h1:first-child{display:none}
h2{font-size:21px;margin:48px 0 14px;padding:0 0 8px;border-bottom:2px solid var(--line);scroll-margin-top:12px}
h2::before{content:"";display:inline-block;width:6px;height:20px;border-radius:3px;background:var(--accent);margin-right:10px;vertical-align:-3px}
h3{font-size:17px;margin:28px 0 8px;color:var(--accent-ink)}
p{margin:10px 0}
ul,ol{padding-left:1.4em}li{margin:4px 0}
a{color:var(--accent-ink);text-underline-offset:3px}
hr{border:none;border-top:1px solid var(--line);margin:28px 0}
code{background:var(--code-bg);border-radius:5px;padding:1px 6px;font-size:.9em;
  font-family:ui-monospace,SFMono-Regular,Menlo,"IPAGothic",monospace;overflow-wrap:anywhere}
pre{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;overflow-x:auto;
  line-height:1.55;font-size:13.5px}
pre code{background:none;padding:0;font-size:inherit;overflow-wrap:normal;font-family:"IPAGothic",ui-monospace,Menlo,monospace}
blockquote{margin:14px 0;padding:10px 16px;background:var(--quote);border-left:4px solid var(--accent);border-radius:0 10px 10px 0}
blockquote p{margin:4px 0}
.scroll{overflow-x:auto;-webkit-overflow-scrolling:touch;margin:12px 0;border:1px solid var(--line);border-radius:12px;background:var(--card)}
table{border-collapse:collapse;width:100%;font-size:14.5px}
th,td{text-align:left;padding:9px 12px;border-bottom:1px solid var(--line);vertical-align:top}
tr:last-child td{border-bottom:none}
th{background:var(--soft);font-size:13px;color:var(--muted);white-space:nowrap}
td:first-child{white-space:nowrap}
.theme{position:fixed;right:14px;bottom:14px;border:1px solid var(--line);background:var(--card);color:var(--ink);
  border-radius:999px;padding:8px 14px;font:inherit;font-size:13px;cursor:pointer;box-shadow:0 4px 14px rgba(0,0,0,.08)}
@media (max-width:600px){.top h1{font-size:23px}h2{font-size:19px}td:first-child{white-space:normal}}
@media print{
  @page{size:A4;margin:16mm 14mm}
  :root{--bg:#fff;--card:#fff;--ink:#111;--muted:#555;--line:#ccc;--soft:#f3f3f3;--code-bg:#f2f2f2;--quote:#fff6ee}
  body{font-size:11pt;line-height:1.65;font-family:"IPAPGothic","IPAGothic",sans-serif}
  .theme,hr{display:none}
  .top{padding:18mm 0 10mm;-webkit-print-color-adjust:exact;print-color-adjust:exact}
  .top .in,main{max-width:none;padding:0}
  h2{break-before:page;margin-top:0;break-after:avoid}
  h3{break-after:avoid}
  pre,blockquote,tr{break-inside:avoid}
  .scroll{overflow:visible;border:none}
  table{font-size:9.5pt}th,td{padding:5px 7px}
  pre{font-size:8.5pt;white-space:pre}
  a{color:inherit;text-decoration:none}
}
"""

COMPACT_CSS = r"""
@media print{
  @page{margin:11mm 12mm}
  body{font-size:9.5pt;line-height:1.5}
  .top{padding:6mm 0 4mm}.top h1{font-size:17pt}.top .k{display:none}
  h2{break-before:auto;margin:5mm 0 2mm;padding-bottom:1mm;font-size:12pt}
  h2::before{height:12pt}
  p,ul,ol{margin:1.5mm 0}li{margin:.5mm 0}
  blockquote{margin:2mm 0;padding:1.5mm 3mm;font-size:8.5pt}
  .scroll{margin:2mm 0}table{font-size:8.5pt}th,td{padding:3px 6px}
  pre{padding:2mm 3mm;font-size:8pt;margin:2mm 0}
}
"""

page = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'%3E%3Ctext y='.9em' font-size='90'%3E%F0%9F%93%96%3C/text%3E%3C/svg%3E">
<style>{CSS}{COMPACT_CSS if COMPACT else ""}</style>
</head>
<body>
<!-- このファイルは {md_name} から自動で作っています。内容を直すときは {md_name} を直してください。 -->
<header class="top"><div class="in"><div class="k">GUIDEBOOK</div><h1>{title}</h1></div></header>
<main>
{body}
</main>
<button class="theme" id="theme" type="button">表示の明暗</button>
<script>
(function(){{var r=document.documentElement,b=document.getElementById('theme');
try{{var s=localStorage.getItem('guide_theme');if(s)r.dataset.theme=s}}catch(e){{}}
b.onclick=function(){{var dark=r.dataset.theme?r.dataset.theme==='dark':matchMedia('(prefers-color-scheme: dark)').matches;
r.dataset.theme=dark?'light':'dark';try{{localStorage.setItem('guide_theme',r.dataset.theme)}}catch(e){{}}}};}})();
</script>
</body>
</html>
"""
open(html_path, 'w', encoding='utf-8').write(page)
print('wrote', html_path)

