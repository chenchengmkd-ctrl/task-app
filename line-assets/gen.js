// LINE用画像の生成（アイコン + リッチメニュー背景）
// 使い方: scratchpad に sharp を入れた状態で  node gen.js
const fs = require("fs");
const path = require("path");
const sharp = require(process.env.SHARP_PATH || "sharp");

const OUT = __dirname;
const JP = "'Yu Gothic','YuGothic','Meiryo','BIZ UDGothic','MS PGothic',sans-serif";

/* ---------- アイコン 640x640 ---------- */
const icon = `
<svg width="640" height="640" viewBox="0 0 640 640" xmlns="http://www.w3.org/2000/svg">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#6172F3"/>
      <stop offset="1" stop-color="#4338CA"/>
    </linearGradient>
  </defs>
  <rect width="640" height="640" rx="150" fill="url(#bg)"/>
  <rect x="180" y="158" width="280" height="344" rx="36" fill="#ffffff"/>
  <rect x="258" y="124" width="124" height="66" rx="22" fill="#DfE3EA"/>
  <rect x="286" y="110" width="68" height="42" rx="16" fill="#C3C9D4"/>
  ${[248, 318, 388].map(y => `
    <circle cx="240" cy="${y}" r="23" fill="#22C55E"/>
    <path d="M229 ${y} l8 9 l15 -18" stroke="#fff" stroke-width="7.5" fill="none" stroke-linecap="round" stroke-linejoin="round"/>
    <rect x="280" y="${y - 11}" width="150" height="22" rx="11" fill="#D5DAE2"/>`).join("")}
</svg>`;

/* ---------- リッチメニュー 2500x843（横3分割） ---------- */
const W = 2500, H = 843, CW = W / 3;
const cells = [
  { label: "一覧", sub: "タスクを見る", color: "#4F46E5", icon: "list" },
  { label: "通知", sub: "期限を確認", color: "#0EA5E9", icon: "bell" },
  { label: "ヘルプ", sub: "使い方", color: "#14B8A6", icon: "help" },
];
function glyph(kind, cx, cy, c) {
  if (kind === "list") {
    let s = "";
    [-70, 0, 70].forEach(dy => {
      s += `<circle cx="${cx - 78}" cy="${cy + dy}" r="13" fill="#fff"/>`;
      s += `<rect x="${cx - 48}" y="${cy + dy - 11}" width="150" height="22" rx="11" fill="#fff"/>`;
    });
    return s;
  }
  if (kind === "bell") {
    return `
      <path d="M${cx} ${cy - 92}
        c -55 0 -82 38 -82 92 c 0 64 -22 80 -36 100 l 236 0
        c -14 -20 -36 -36 -36 -100 c 0 -54 -27 -92 -82 -92 z"
        fill="#fff"/>
      <circle cx="${cx}" cy="${cy + 120}" r="26" fill="#fff"/>`;
  }
  // help: ? mark
  return `<text x="${cx}" y="${cy + 58}" font-family="${JP}" font-size="200" font-weight="800"
            fill="#fff" text-anchor="middle">?</text>`;
}
const rich = `
<svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" xmlns="http://www.w3.org/2000/svg">
  <rect width="${W}" height="${H}" fill="#F7F8FB"/>
  ${cells.map((c, i) => {
    const cx = CW * i + CW / 2, cyIcon = 330;
    const div = i > 0 ? `<rect x="${CW * i - 2}" y="120" width="4" height="${H - 240}" rx="2" fill="#E6E9F0"/>` : "";
    return `
    ${div}
    <circle cx="${cx}" cy="${cyIcon}" r="150" fill="${c.color}"/>
    ${glyph(c.icon, cx, cyIcon, c.color)}
    <text x="${cx}" y="640" font-family="${JP}" font-size="104" font-weight="800"
          fill="#1F2430" text-anchor="middle">${c.label}</text>
    <text x="${cx}" y="712" font-family="${JP}" font-size="46" font-weight="500"
          fill="#8A90A0" text-anchor="middle">${c.sub}</text>`;
  }).join("")}
</svg>`;

(async () => {
  await sharp(Buffer.from(icon)).png().toFile(path.join(OUT, "icon.png"));
  await sharp(Buffer.from(rich)).png().toFile(path.join(OUT, "richmenu.png"));
  fs.writeFileSync(path.join(OUT, "_icon.svg"), icon);
  fs.writeFileSync(path.join(OUT, "_richmenu.svg"), rich);
  console.log("done: icon.png, richmenu.png");
})();
