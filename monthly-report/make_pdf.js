// 月次レポートのHTML（アーティファクトと同じもの）をA4のPDFにする。
// 使い方: node monthly-report/make_pdf.js レポート.html 出力.pdf
// 画面用のHTMLをそのまま使い、印刷用の調整（白背景・表やグラフをページ途中で切らない等）をここで足す。
const path = require('path');
const { chromium } = require('playwright');

const PRINT_CSS = `
  :root{--bg:#fff!important}
  body{font-size:13px!important;-webkit-print-color-adjust:exact;print-color-adjust:exact}
  .wrap{padding:0!important;gap:26px!important;max-width:none!important}
  .tbl.wide table,.chart svg{min-width:0!important}
  .tbl,.chart{overflow:visible!important}
  .stat,.tbl,.chart,.box,.hbars,.points{break-inside:avoid}
  h2{break-after:avoid}
`;

(async () => {
  const [src, out] = process.argv.slice(2);
  if (!src || !out) {
    console.error('使い方: node make_pdf.js レポート.html 出力.pdf');
    process.exit(1);
  }
  const browser = await chromium.launch();
  const page = await browser.newPage();
  await page.emulateMedia({ colorScheme: 'light' });
  await page.goto('file://' + path.resolve(src), { waitUntil: 'networkidle' });
  await page.addStyleTag({ content: PRINT_CSS });
  await page.evaluate(() => document.fonts.ready);
  await page.pdf({
    path: out, format: 'A4', printBackground: true,
    margin: { top: '14mm', bottom: '14mm', left: '12mm', right: '12mm' },
    displayHeaderFooter: true, headerTemplate: '<span></span>',
    footerTemplate: '<div style="font-size:8px;width:100%;text-align:center;color:#888"><span class="pageNumber"></span> / <span class="totalPages"></span></div>',
  });
  await browser.close();
  console.log('PDFを作りました: ' + out);
})();
