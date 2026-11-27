function reportText(){
  // ハンバーグ日和の日報（この店のLINEの日報の書き方をもとにした形）
  const SEP='＝＝＝＝＝＝＝＝＝＝＝＝＝';
  const d=new Date(R.date+'T00:00');const wd='日月火水木金土'[d.getDay()];
  const sq=SQ&&SQ.date===R.date?SQ.orders:null;
  // 客数：Airレジの会計があればそこから（店内とお弁当・テイクアウトを分ける）。無ければ記録した組から
  let inG=0,inN=0,boG=0,boN=0;
  if(sq){sq.forEach(o=>{const n=+o.guests||1;if(o.bento){boG++;boN+=n}else{inG++;inN+=n}})}
  else R.customers.forEach(c=>{const n=+c.n||1;if(c.takeout){boG++;boN+=n}else{inG++;inN+=n}});
  // メニュー別：Airレジの注文から数える。無ければ記録した組の「食べたもの」から
  const mc={};MENU.forEach(k=>mc[k]=0);
  if(sq)sq.forEach(o=>{const m=mapItems(o.items);Object.keys(m.ord).forEach(k=>mc[k]=(mc[k]||0)+m.ord[k])});
  else R.customers.forEach(c=>Object.keys(c.ord||{}).forEach(k=>mc[k]=(mc[k]||0)+(+c.ord[k]||0)));
  const kids=+R.kids||0;
  const L=[`【日報】ハンバーグ日和 ${d.getMonth()+1}月${d.getDate()}日（${wd}）`,`天気：${R.weather||''}　記入者：${R.who||''}`,SEP,
    '▼売上（税込）',`総売上：${yen(R.sales)}`,`　うち現金：${yen(R.cash)}`,`累計売上：${yen(R.cum)}`,
    `デリバリー（別途）：Uber ${yen(R.uber)} ／ ロケットナウ ${yen(R.rocket)}`,SEP,
    '▼客数',`店内：${inG}組 ${inN}名${kids?`（お子様${kids}名）`:''}`,`お弁当・テイクアウト：${boG}組 ${boN}名`,SEP,
    '▼メニュー別',MENU.map(k=>`${k}：${mc[k]||0}`).join(' ／ '),SEP,'▼来店経路'];
  const sc=srcCounts();
  const RS=Object.keys(sc).filter(s=>sources().includes(s)||sc[s]);
  const W=Math.max(...RS.map(x=>[...x].length));
  const zen=t=>t.replace(/[!-~]/g,ch=>String.fromCharCode(ch.charCodeAt(0)+0xFEE0)).replace(/ /g,'　');
  RS.forEach(s=>{const name=zen(s);L.push(name+'　'.repeat(W-[...name].length)+`：${sc[s]}人`)}); // 0人の項目も出す
  L.push(SEP,'▼営業の流れ');if((R.flow||'').trim())L.push(R.flow.trim());
  L.push('','▼お客様の様子・反応');
  const only=ls.get('nippoair_repmode','all')==='detail';
  R.customers.slice().reverse()
    .filter(c=>!only||(c.comment||'').trim()||c.label||(c.sex||[]).length||(c.age||[]).length||c.regId)
    .forEach(c=>{const o=ordLine(c);L.push('',custTitle(c),((c.comment||'').trim()+(o?`（${o}）`:'')).trim())});
  L.push('','▼気づき・課題');if((R.handover||'').trim())L.push(R.handover.trim());
  L.push('','▼インバウンド');
  const ib={};R.customers.forEach(c=>{if(c.country||isInbound([c.label]))ib[c.country||'国不明']=(ib[c.country||'国不明']||0)+(+c.n||1)});
  const ibk=Object.keys(ib).sort((a,b)=>(a==='国不明')-(b==='国不明')||ib[b]-ib[a]);
  if(ibk.length)L.push('ご来店：'+ibk.map(k=>k+' '+ib[k]+'名').join('・'));
  if((R.inbound||'').trim())L.push(R.inbound.trim());
  L.push('',`口コミ依頼できた人数：${+R.review||0}人`);
  return L.join('\n');
}
