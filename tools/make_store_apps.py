"""他店舗用の日報アプリを、今の店の nippo.html から作る。

  python3 tools/make_store_apps.py

nippo.html を直したら、これを実行し直すと nippo-air.html / nippo-usen.html にも反映される。
置き換える場所が見つからないとエラーで止まる（nippo.html 側の書き方が変わったときは、ここを合わせる）。

- nippo-air.html  … Airレジの店。売上は手入力（AirレジのAPI仕様書が届いたら自動取り込みにする）
- nippo-usen.html … USENレジの店。USENレジの管理画面から書き出したCSV（伝票データ）を読み込む
どちらも、ブラウザの保存場所・サーバーの同期の保存場所を今の店と分け、
LINEは勤怠Bot（今の店のグループに投稿する）ではなく共有メニューで送る。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Doc:
    def __init__(self, text):
        self.s = text

    def rep(self, old, new, count=1):
        n = self.s.count(old)
        if n != count:
            sys.exit(f'置き換える場所が {n} か所あります（{count} か所のはず）:\n{old[:120]}')
        self.s = self.s.replace(old, new)


def common(d, ls_prefix, sync_store, title, reg_name):
    """どの店にも共通の変更。"""
    d.s = d.s.replace("'nippo_", f"'{ls_prefix}_")  # 同じサイト上の今の店のアプリと、端末内の保存場所を分ける
    d.rep('<meta name="apple-mobile-web-app-title" content="日報">',
          f'<meta name="apple-mobile-web-app-title" content="日報（{title}）">')
    d.rep('<title>日報</title>', f'<title>日報（{title}）</title>')
    d.rep('<h1>日報<small id="hDate"></small></h1>', f'<h1>日報<small>{title}</small><small id="hDate"></small></h1>')
    # 売上をレジの精算画面と見比べたチェック（入れないと送れない）
    d.rep('<div class="src-grid" id="srcSum"></div>', f'''<div class="src-grid" id="srcSum"></div>
    <label class="regchk"><input type="checkbox" id="regChk"> {reg_name}の精算画面と見比べて、売上・客数が合っていることを確認しました</label>''')
    d.rep("  .hint.warn{", "  .regchk{display:flex;gap:10px;align-items:flex-start;margin-top:14px;padding:12px;border:1.5px solid var(--line);border-radius:12px;font-size:14px;font-weight:700;line-height:1.5}\n"
          "  .regchk input{width:22px;height:22px;flex:0 0 22px;margin-top:1px}\n  .hint.warn{")
    d.rep("""function check(){
  const left=SQ&&SQ.date===R.date?unassigned().length:0;""", f"""function check(){{
  if(!R.regChecked){{toast('「営業おわり」の画面で、{reg_name}の精算画面と見比べたチェックを入れてください');go('end');return false}}
  const left=SQ&&SQ.date===R.date?unassigned().length:0;""")
    d.rep("""$('cumBase').addEventListener('input',""", """$('regChk').addEventListener('change',()=>{R.regChecked=$('regChk').checked;save()});
// 売上・客数を書き換えたら、もう一度見比べてもらう
['sales','cum'].forEach(f=>$(f).addEventListener('input',()=>{if(R.regChecked){R.regChecked=false;$('regChk').checked=false;save()}}));
$('cumBase').addEventListener('input',""")
    d.rep("function renderSales(){", "function renderSales(){\n  $('regChk').checked=!!R.regChecked;")
    # 設定：Squareの説明を外す（合言葉はスマホ同期にだけ使う）
    d.rep("<h2>Square連携・スマホ同期</h2>", "<h2>スマホ同期</h2>")
    d.rep("""<div class="hint">入れると、本日売上と会計ごとの注文がSquareから自動で入ります。<br>スマホでも""", """<div class="hint">スマホでも""")
    # サーバーの同期は、店ごとの保存場所に
    d.rep("var SYNC_API='https://task-app-nine-liard.vercel.app/api/nippo_sync';",
          f"var SYNC_API='https://task-app-nine-liard.vercel.app/api/nippo_sync',SYNC_STORE='{sync_store}'; // 今の店（鰻）と別の場所に保存する")
    d.rep("body:JSON.stringify(body)});\n  const j=await res.json().catch(()=>({}));",
          "body:JSON.stringify(Object.assign({store:SYNC_STORE},body))});\n  const j=await res.json().catch(()=>({}));")
    # LINE：勤怠Botは今の店のグループに投稿するので使わない
    d.rep("  if(!key){shareSend(t);return}",
          "  shareSend(t);return; // この店では勤怠Botを使わない（今の店のグループに投稿されるため）。共有メニュー・コピーで送る\n  if(!key){shareSend(t);return}")


def make_air(src):
    d = Doc(src)
    common(d, 'nippoair', 'air', 'Airレジ店', 'Airレジ')
    d.rep('<button class="link" id="btnSq">Square更新</button>', '<button class="link" id="btnSq" style="display:none">Square更新</button>')
    d.rep('<div id="sqBox"></div>', '<div id="sqBox" style="display:none"></div>')
    d.rep("async function fetchSquare(manual){",
          "// Airレジ店：レジとの自動連携は、Airレジの「API仕様書」が届くまでお休み（売上は手入力）。\n"
          "// 仕様書が届いたら、ここでAirレジの売上・会計を取り込むようにする\nasync function fetchSquare(manual){return;")
    d.rep("function renderSqBox(){\n  const box=$('sqBox');box.innerHTML='';", "function renderSqBox(){\n  const box=$('sqBox');box.innerHTML='';return;")
    d.rep('<h2>実績<span class="sub">最後の確認用。売上はSquareから自動で入ります</span></h2>',
          '<h2>実績<span class="sub">売上はAirレジの精算画面を見て入力します</span></h2>')
    d.rep("h.textContent=ls.get('nippoair_sqkey','')?'':'設定で合言葉を入れるとSquareから自動で入ります';",
          "h.textContent='Airレジの精算画面の売上（税込）を入れます';")
    return d.s


# USENレジのCSV（伝票データ）を読む。列の名前は店の設定で違うことがあるので、よくある名前から探す
USEN_CSV_JS = r"""
/* ---------- USENレジのCSV（伝票データ）を読み込む ----------
   USENレジの管理画面「帳票管理」→「汎用検索」→ データ形式「売上データ(伝票)」→「CSV出力」したファイル。
   読み込んだ会計は、Squareの会計と同じ一覧に並び、押して記録できる。読み込み直しても二重にならない
   （伝票番号で見分ける）。列の名前は店の設定で違うことがあるので、よくある名前から探す。 */
const CSV_COLS={
  id:/伝票(番号|No|NO|ＮＯ|ID)|会計(番号|No|ID)|取引(番号|ID)|レシート(番号|No)|^伝票$/,
  date:/^(営業日|会計日|売上日|日付|伝票日付|精算日)$/,
  time:/会計(日時|時刻|時間)|精算(日時|時刻)|伝票(日時|時刻)|取引日時|^日時$|^時刻$|^時間$|退店|会計$/,
  total:/伝票合計|会計合計|合計金額|総合計|お会計|支払合計|売上合計|税込合計|^合計$/,
  amount:/^金額$|小計|売上金額|税込金額|商品金額|明細金額/,
  name:/商品名|メニュー名|品名|^メニュー$|^商品$/,
  qty:/^数量$|個数|^点数$/,
  table:/テーブル|卓/,
  guests:/客数|人数/,
  status:/取消|キャンセル|状態|区分/};
function csvDecode(buf){try{return new TextDecoder('utf-8',{fatal:true}).decode(buf)}catch(e){return new TextDecoder('shift_jis').decode(buf)}}
function csvRows(text){const rows=[];let row=[],cur='',q=false;text=text.replace(/^﻿/,'');
  for(let i=0;i<text.length;i++){const c=text[i];
    if(q){if(c==='"'){if(text[i+1]==='"'){cur+='"';i++}else q=false}else cur+=c}
    else if(c==='"')q=true;else if(c===','){row.push(cur);cur=''}
    else if(c==='\n'||c==='\r'){if(c==='\r'&&text[i+1]==='\n')i++;row.push(cur);rows.push(row);row=[];cur=''}
    else cur+=c}
  if(cur||row.length){row.push(cur);rows.push(row)}
  return rows.map(r=>r.map(x=>x.trim())).filter(r=>r.some(Boolean))}
const csvNum=v=>{const n=Number(String(v||'').replace(/[¥￥,，円\s]/g,'').replace(/^\((.*)\)$/,'-$1'));return isNaN(n)?0:n};
function csvDate(v){const m=String(v||'').match(/(\d{4})[\/\-年.](\d{1,2})[\/\-月.](\d{1,2})/);return m?m[1]+'-'+String(m[2]).padStart(2,'0')+'-'+String(m[3]).padStart(2,'0'):''}
function csvTime(v){const m=String(v||'').match(/(\d{1,2}):(\d{2})/);return m?String(m[1]).padStart(2,'0')+':'+m[2]:''}
// CSVの文字列 → {date, total, orders:[{id,time,total,items,table,guests}], dates:[CSVに入っていた日付]}
function parseUsenCsv(text,want){
  const rows=csvRows(text);
  const hi=rows.findIndex(r=>r.filter(h=>Object.values(CSV_COLS).some(rx=>rx.test(h))).length>=2);
  if(hi<0)throw new Error('見出しの行（伝票番号・金額など）が見つかりません');
  const head=rows[hi],col={};
  Object.entries(CSV_COLS).forEach(([k,rx])=>{const i=head.findIndex(h=>rx.test(h));if(i>=0)col[k]=i});
  if(col.total==null&&col.amount==null)throw new Error('金額の列が見つかりません（見出し：'+head.join('、')+'）');
  const get=(r,k)=>col[k]==null?'':(r[col[k]]||'');
  const by=new Map(),dates=new Set();
  rows.slice(hi+1).forEach((r,ri)=>{
    if(/取消|キャンセル|返品|削除|VOID/i.test(get(r,'status')))return;
    const d=csvDate(get(r,'date'))||csvDate(get(r,'time'));if(d)dates.add(d);
    if(want&&d&&d!==want)return;
    const id=get(r,'id')||('row'+ri);
    if(!by.has(id))by.set(id,{id:'usen-'+id,time:'',total:0,_tot:null,items:[],table:'',guests:0});
    const o=by.get(id);
    o.time=o.time||csvTime(get(r,'time'));o.table=o.table||get(r,'table');
    if(col.guests!=null)o.guests=Math.max(o.guests,csvNum(get(r,'guests')));
    if(col.total!=null){const t=csvNum(get(r,'total'));if(t)o._tot=Math.max(o._tot||0,t)} // 伝票合計は明細の行ごとに同じ値が入るので、足さない
    else o.total+=csvNum(get(r,'amount'));
    const nm=get(r,'name');if(nm)o.items.push({name:nm,qty:col.qty!=null?(csvNum(get(r,'qty'))||1):1})});
  const orders=[...by.values()].map(o=>{if(o._tot!=null)o.total=o._tot;delete o._tot;return o}).filter(o=>o.total||o.items.length)
    .sort((a,b)=>(a.time||'').localeCompare(b.time||''));
  return {orders,total:orders.reduce((s,o)=>s+o.total,0),dates:[...dates].sort()}}
// 「売上管理」→「伝票明細」の画面を印刷→PDFで保存したもの（またはその画面の文字）から会計を拾う。
// 1件ごとに「伝票番号 … テーブル/T3 … 会計済 … 金額:¥4,100 … 人数:1 … 営業日:2026/11/23 … 退店:2026/11/23 14:30」と並んでいる
function parseUsenSlipText(text,want){
  const flat=String(text).replace(/\s+/g,' '),rx=/(?:^|[^\d])(20\d{5})\s?(\d{8})(?!\d)/g,hits=[];let m;
  while((m=rx.exec(flat)))hits.push({id:m[1]+m[2],at:m.index+m[0].length});
  const orders=[],dates=new Set(),seen=new Set();
  hits.forEach((h,i)=>{
    const seg=flat.slice(h.at,i+1<hits.length?hits[i+1].at:flat.length),tight=seg.replace(/\s+/g,'');
    const amt=tight.match(/金額:?[¥￥]?([\d,]+)/);if(!amt)return; // 金額のない番号（検索欄の日付など）は会計ではない
    if(seen.has(h.id))return;seen.add(h.id);
    if(/取消|キャンセル|返品|削除/.test(tight)&&!/会計済/.test(tight))return;
    const day=seg.match(/営\s*業\s*日\s*:?\s*(\d{4}\/\d{1,2}\/\d{1,2})/),out=seg.match(/退\s*店\s*:?\s*(\d{4}\/\d{1,2}\/\d{1,2})\s*(\d{1,2}:\d{2})/),
      inn=seg.match(/入\s*店\s*:?\s*(\d{4}\/\d{1,2}\/\d{1,2})\s*(\d{1,2}:\d{2})/);
    const d=csvDate(day?day[1]:out?out[1]:inn?inn[1]:'');if(d)dates.add(d);if(want&&d&&d!==want)return;
    const tb=tight.match(/(テーブル|カウンター|座敷|個室|テラス|テイクアウト|持ち帰り)[^\/]{0,6}\/([A-Za-z0-9０-９\-]+)/),ppl=tight.match(/人数:?(\d+)/);
    orders.push({id:'usen-'+h.id,time:csvTime(out?out[2]:inn?inn[2]:''),total:csvNum(amt[1]),items:[],table:tb?tb[2]:'',guests:ppl?+ppl[1]:0,
      takeout:!!(tb&&/テイクアウト|持ち帰り/.test(tb[1]))})});
  orders.sort((a,b)=>(a.time||'').localeCompare(b.time||''));
  return {orders,total:orders.reduce((s,o)=>s+o.total,0),dates:[...dates].sort()}}
// PDFの文字を取り出す（pdf.js を必要なときだけ読み込む）
async function pdfText(buf){
  if(!window.pdfjsLib)await new Promise((ok,ng)=>{const sc=document.createElement('script');sc.src='https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.min.js';sc.onload=ok;sc.onerror=()=>ng(new Error('PDFを読む部品を読み込めませんでした（電波を確認してください）'));document.head.appendChild(sc)});
  pdfjsLib.GlobalWorkerOptions.workerSrc='https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js';
  const pdf=await pdfjsLib.getDocument({data:new Uint8Array(buf)}).promise,out=[];
  for(let i=1;i<=pdf.numPages;i++){const c=await (await pdf.getPage(i)).getTextContent();out.push(c.items.map(x=>x.str+(x.hasEOL?'\n':' ')).join(''))}
  return out.join('\n')}
// 読み込んだ会計は日付ごとに端末に残す（開き直しても一覧が消えないように。1週間分）
const csvStore=()=>ls.get('nippousen_csv',{});
function csvSave(date,sq){const a=csvStore();a[date]=sq;Object.keys(a).sort().slice(0,-7).forEach(k=>delete a[k]);ls.set('nippousen_csv',a)}
async function importCsv(file){
  try{
    const buf=await file.arrayBuffer(),head=new Uint8Array(buf.slice(0,4)),sig=String.fromCharCode(...head);
    // CSV以外を選んだときは、何のファイルか教える（iPadではCSV以外も選べるようにしてある）
    if(sig.startsWith('PK')){toast(`「${file.name}」はExcelのファイルです。CSVか、伝票明細を印刷→PDFで保存したものを選んでください`);return}
    let p;
    if(sig==='%PDF'){toast('PDFを読み込んでいます…');p=parseUsenSlipText(await pdfText(buf),R.date);if(!p.orders.length&&!p.dates.length)throw new Error('PDFの中に伝票（伝票番号と金額）が見つかりません。「売上管理」→「伝票明細」の一覧を印刷→PDFで保存したものを選んでください')}
    else{const text=csvDecode(buf);try{p=parseUsenCsv(text,R.date)}catch(e){p=parseUsenSlipText(text,R.date);if(!p.orders.length&&!p.dates.length)throw e}}
    if(!p.orders.length){
      const other=p.dates.filter(d=>d!==R.date);
      if(other.length){toast(`このCSVは ${other.map(d=>+d.slice(5,7)+'/'+(+d.slice(8,10))).join('・')} の分です（${+R.date.slice(5,7)}/${+R.date.slice(8,10)}の会計は入っていません）`);return}
      toast('会計が見つかりませんでした');return}
    SQ={total:p.total,count:p.orders.length,orders:p.orders,at:new Date().toTimeString().slice(0,5),date:R.date};csvSave(R.date,SQ);
    R.sales=String(p.total);$('sales').value=R.sales;autoCum();save(true);
    qAuto=true;renderSqBox();renderSales();toast(`読み込みました：会計${p.orders.length}件・${yen(p.total)}`);
  }catch(e){toast('CSVを読めませんでした：'+(e.message||e))}
}
"""


def make_usen(src):
    d = Doc(src)
    common(d, 'nippousen', 'usen', 'USENレジ店', 'USENレジ')
    d.rep('<button class="link" id="btnSq">Square更新</button>',
          '<button class="link" id="btnSq">読込（PDF・CSV）</button><input type="file" id="csvFile" style="display:none">')
    d.rep('<h2>実績<span class="sub">最後の確認用。売上はSquareから自動で入ります</span></h2>',
          '<h2>実績<span class="sub">売上はUSENレジのCSVから入ります（手で直すこともできます）</span></h2>')
    d.rep("h.textContent=ls.get('nippousen_sqkey','')?'':'設定で合言葉を入れるとSquareから自動で入ります';",
          "h.textContent='「会計を記録」の「読込」で伝票明細のPDFかCSVを読み込むと入ります';")
    # Squareを読む代わりに、その日に読み込んだCSVの会計を出す
    d.rep("async function fetchSquare(manual){",
          "// USENレジ店：Squareの代わりに、その日に読み込んだCSVの会計を出す（ネットには取りに行かない）\n"
          "async function fetchSquare(manual){const st=csvStore()[R.date];SQ=st||null;renderSqBox();renderSales();return;")
    d.rep("function renderSqBox(){\n  const box=$('sqBox');box.innerHTML='';\n  if(!ls.get('nippousen_sqkey','')){",
          "function renderSqBox(){\n  const box=$('sqBox');box.innerHTML='';\n"
          "  if(!SQ){box.innerHTML='<div class=\"hint\" style=\"margin:-4px 0 10px\">USENレジの管理画面の「売上管理」→「伝票明細」で今日の一覧を出し、共有（プリント）→ PDFで「ファイル」に保存して、右上の「読込」で選びます（「帳票管理」→「汎用検索」のCSVも読めます）。営業中に何度読み込み直しても、記録した会計は二重になりません。会計なしでも記録はできます</div>';return}\n"
          "  if(false){")
    d.rep("h.textContent=`未記録の会計はありません（Square ${SQ.orders.length}件・${SQ.at}更新）`",
          "h.textContent=`未記録の会計はありません（CSV ${SQ.orders.length}件・${SQ.at}読込）`")
    d.rep("h.textContent=`未記録 ${list.length}件（Square ${SQ.orders.length}件・${SQ.at}更新）。",
          "h.textContent=`未記録 ${list.length}件（CSV ${SQ.orders.length}件・${SQ.at}読込）。")
    d.rep("h.textContent=`Squareから自動（${SQ.at}更新・会計${SQ.count}件）`;", "h.textContent=`USENレジのCSVから（${SQ.at}読込・会計${SQ.count}件）`;")
    # 会計の一覧にテーブル番号も出す。会計を選んだら、CSVの客数を人数に入れる
    d.rep("b.innerHTML=`<b>${esc(o.time)}</b> ${yen(o.total)}",
          "b.innerHTML=`<b>${esc(o.time)}</b>${o.table?' '+esc(o.table):''} ${yen(o.total)}")
    d.rep("  qOrd=ord;qOther=other.join('・');\n  if(qSqs.length){qTake=take;",
          "  qOrd=ord;qOther=other.join('・');\n"
          "  const g=qSqs.reduce((s,id)=>{const o=SQ&&SQ.orders.find(x=>x.id===id);return s+(o&&+o.guests||0)},0);if(g&&g!==qN){qN=g;fitMap(qSrcs,qN);setTimeout(renderQuick,0)} // CSVに客数があれば人数に入れる（一覧を描いている途中なので、描き直しは後で）\n"
          "  if(qSqs.length){qTake=take;")
    d.rep("$('btnSq').onclick=()=>fetchSquare(true);",
          "$('btnSq').onclick=()=>$('csvFile').click();\n$('csvFile').onchange=e=>{const f=e.target.files[0];e.target.value='';if(f)importCsv(f)};")
    d.rep("/* ---------- 画面描画 ---------- */", USEN_CSV_JS + "\n/* ---------- 画面描画 ---------- */")
    return d.s


def main():
    src = open(os.path.join(ROOT, 'nippo.html'), encoding='utf-8').read()
    for name, fn in (('nippo-air.html', make_air), ('nippo-usen.html', make_usen)):
        out = fn(src)
        with open(os.path.join(ROOT, name), 'w', encoding='utf-8') as f:
            f.write(out)
        print('wrote', name)


if __name__ == '__main__':
    main()
