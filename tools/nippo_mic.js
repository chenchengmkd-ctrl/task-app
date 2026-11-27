/* ---------- 音声入力（🎤ボタン） ----------
   文字の欄の右下に🎤を付ける。押して話すと、欄の最後に文字が入る（iPhone・iPadのSafariの音声認識）。
   使えない端末では、キーボードのマイクを使うよう案内する */
const SpeechRec=window.SpeechRecognition||window.webkitSpeechRecognition;
let micBusy=null;
function addMic(el){
  if(!el||el.dataset.mic)return;el.dataset.mic='1';
  const wrap=document.createElement('div');wrap.className='mic-wrap';el.parentNode.insertBefore(wrap,el);wrap.appendChild(el);
  const b=document.createElement('button');b.type='button';b.className='mic-btn';b.textContent='🎤';b.setAttribute('aria-label','音声で入力');wrap.appendChild(b);
  b.onclick=()=>{
    if(!SpeechRec){toast('この端末では、キーボードのマイク🎤から話して入力してください');el.focus();return}
    if(micBusy){micBusy.stop();return}
    const r=new SpeechRec();r.lang='ja-JP';r.interimResults=false;r.continuous=false;micBusy=r;b.classList.add('on');
    r.onresult=e=>{const t=[...e.results].map(x=>x[0].transcript).join('').trim();if(!t)return;
      const v=el.value.replace(/\s+$/,'');el.value=v?(/[。！？」]$/.test(v)?v+t:v+(el.tagName==='TEXTAREA'?'。':'・')+t):t;el.dispatchEvent(new Event('input'))};
    r.onerror=e=>{if(e.error==='not-allowed')toast('マイクの使用を許可してください（設定→Safari→マイク）')};
    r.onend=()=>{micBusy=null;b.classList.remove('on')};
    try{r.start()}catch(e){micBusy=null;b.classList.remove('on')}
  };
}
