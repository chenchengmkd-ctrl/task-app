// ガイドブックのHTMLを A4 の PDF にする。使い方: node tools/guide_pdf.js nippo-guide.html NIPPO_GUIDE.pdf
const path = require('path');
const { chromium } = require('playwright');
(async () => {
  const [html, pdf] = process.argv.slice(2);
  const browser = await chromium.launch();
  const page = await browser.newPage();
  await page.goto('file://' + path.resolve(html));
  await page.emulateMedia({ media: 'print' });
  await page.pdf({
    path: pdf, format: 'A4', printBackground: true, preferCSSPageSize: true,
    displayHeaderFooter: true, headerTemplate: '<span></span>',
    footerTemplate: '<div style="font-size:8px;width:100%;text-align:center;color:#888"><span class="pageNumber"></span> / <span class="totalPages"></span></div>',
  });
  await browser.close();
})();
