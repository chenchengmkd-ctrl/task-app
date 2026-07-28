// LINE公式アカウントのアイコン（落ち着いた濃紺＋金）を生成する。
// 使い方: sharp を入れた状態で  SHARP_PATH=<sharpのパス> node gen-icon-v2.js
// 配色はリッチメニュー（linebot/richmenu.py で生成する画像）と揃えてある。
const fs = require('fs');
const path = require('path');
const sharp = require(process.env.SHARP_PATH || 'sharp');

const S = 640;
const svg = `
<svg width="${S}" height="${S}" viewBox="0 0 ${S} ${S}" xmlns="http://www.w3.org/2000/svg">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#2b333b"/>
      <stop offset="1" stop-color="#171b1f"/>
    </linearGradient>
  </defs>
  <rect width="${S}" height="${S}" fill="url(#bg)"/>

  <!-- クリップボード本体 -->
  <rect x="196" y="168" width="248" height="304" rx="34" fill="#f2efe9"/>
  <rect x="266" y="136" width="108" height="60" rx="20" fill="#c9c3b6"/>
  <rect x="292" y="122" width="56" height="38" rx="14" fill="#a9a294"/>

  <!-- チェック3行（1行目だけ金色＝今日やること） -->
  ${[248, 320, 392].map((y, i) => {
    const on = i === 0;
    return `
    <circle cx="256" cy="${y}" r="21" fill="${on ? '#e0a44a' : '#dcd6ca'}"/>
    <path d="M245 ${y} l8 9 l15 -18" stroke="#ffffff" stroke-width="7" fill="none"
          stroke-linecap="round" stroke-linejoin="round"/>
    <rect x="294" y="${y - 10}" width="${on ? 118 : 100}" height="20" rx="10"
          fill="${on ? '#3a4149' : '#cfc9bd'}"/>`;
  }).join('')}
</svg>`;

(async () => {
  await sharp(Buffer.from(svg)).png().toFile(path.join(__dirname, 'icon-v2.png'));
  fs.writeFileSync(path.join(__dirname, '_icon-v2.svg'), svg);
  console.log('done: icon-v2.png');
})();
