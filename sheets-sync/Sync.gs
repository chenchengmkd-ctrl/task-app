/**
 * タスク同期エンドポイント（GAS）
 * ブラウザのタスク管理アプリから送られたタスクを
 * Googleスプレッドシートに書き込む（毎回まるごと置き換え＝同期）。
 *
 * LINEは不要。スプレッドシートに自動で書き出すためだけのもの。
 */

// ===== ① 自分のスプレッドシートIDを入れる ==========================
const SPREADSHEET_ID = 'ここにスプレッドシートのIDを貼り付け';
const SHEET_NAME     = 'tasks';
const LINE_SHEET     = 'line_tasks';   // LINEで追加したタスク（LINE連携と同じ表）
const REC_SHEET      = 'recurring';    // 定期タスク（LINE側で読む用）
// ==================================================================

const HEADER     = ['id', 'タイトル', '状態', '優先度', '期限', '見積もり', '繰り返し', '進捗メモ', 'タグ', '削除', '更新日時'];
const REC_HEADER = ['タイトル', '繰り返し', '次回予定', '優先度', '見積もり', '更新日時'];

/** アプリからの送信（POST）を受け取る */
function doPost(e) {
  const result = { ok: false };
  try {
    const data = JSON.parse(e.postData.contents);

    // ── LINE取り込みの確認（取り込んだ分を受信箱から外す）──
    if (data.action === 'ack') {
      result.acked = ackLineTasks(data.ids || []);
      result.ok = true;
      return json(result);
    }

    // ── 通常の同期（毎回まるごと置き換え）──
    const tasks = data.tasks || [];
    const sheet = getSheet();
    if (sheet.getLastRow() > 1) {
      sheet.getRange(2, 1, sheet.getLastRow() - 1, HEADER.length).clearContent();
    }
    const now = new Date();
    const rows = tasks.map(t => [
      t.id || '', t.title, t.status, t.priority, t.due,
      t.estimate, t.recurrence, t.note, t.tags,
      t.deleted || 'false',
      t.updated ? new Date(Number(t.updated)) : now
    ]);
    if (rows.length) sheet.getRange(2, 1, rows.length, HEADER.length).setValues(rows);

    // 定期タスクも同期（送られてきた時だけ）
    if (Array.isArray(data.recurring)) writeRecurring(data.recurring, now);

    result.ok = true;
    result.count = rows.length;
  } catch (err) {
    result.error = String(err);
  }
  return json(result);
}

/** ブラウザからの取得（GET）。JSONP対応 */
function doGet(e) {
  const p = (e && e.parameter) || {};

  // LINEで追加したタスクを返す
  if (p.action === 'pull') {
    const payload = { ok: true, tasks: getActiveLineTasks() };
    if (p.callback) {
      return ContentService.createTextOutput(p.callback + '(' + JSON.stringify(payload) + ')')
        .setMimeType(ContentService.MimeType.JAVASCRIPT);
    }
    return json(payload);
  }

  // 全タスク・定期タスクを返す（PC/スマホ間のクラウド同期用）
  if (p.action === 'all') {
    const payload = { ok: true, tasks: getAllTasks(), recurring: getAllRecurring() };
    if (p.callback) {
      return ContentService.createTextOutput(p.callback + '(' + JSON.stringify(payload) + ')')
        .setMimeType(ContentService.MimeType.JAVASCRIPT);
    }
    return json(payload);
  }

  return ContentService.createTextOutput('OK: タスク同期エンドポイントです。');
}

/** LINEで追加された未処理タスクを取得（line_tasks の done=false） */
function getActiveLineTasks() {
  const sh = SpreadsheetApp.openById(SPREADSHEET_ID).getSheetByName(LINE_SHEET);
  if (!sh || sh.getLastRow() < 2) return [];
  // 列: id, userId, title, due, done, created
  const rows = sh.getDataRange().getValues();
  const out = [];
  for (let i = 1; i < rows.length; i++) {
    const r = rows[i];
    const done = r[4] === true || r[4] === 'TRUE' || r[4] === 'true';
    if (done) continue;
    out.push({ id: String(r[0]), title: String(r[2]), due: toISO(r[3]) });
  }
  return out;
}

/** 期限を YYYY-MM-DD に整える（Date / 文字列どちらでも対応） */
function toISO(v) {
  if (!v) return '';
  const p = n => String(n).padStart(2, '0');
  if (Object.prototype.toString.call(v) === '[object Date]') {
    return v.getFullYear() + '-' + p(v.getMonth() + 1) + '-' + p(v.getDate());
  }
  const m = String(v).trim().match(/^(\d{4})[-\/](\d{1,2})[-\/](\d{1,2})/);
  return m ? m[1] + '-' + p(m[2]) + '-' + p(m[3]) : '';
}

/** アプリが取り込んだLINEタスクを done=true にして受信箱から外す */
function ackLineTasks(ids) {
  if (!ids || !ids.length) return 0;
  const sh = SpreadsheetApp.openById(SPREADSHEET_ID).getSheetByName(LINE_SHEET);
  if (!sh || sh.getLastRow() < 2) return 0;
  const set = {};
  ids.forEach(id => { set[String(id)] = true; });
  const rows = sh.getDataRange().getValues();
  let n = 0;
  for (let i = 1; i < rows.length; i++) {
    if (set[String(rows[i][0])]) { sh.getRange(i + 1, 5).setValue(true); n++; }
  }
  return n;
}

/** タスクシートの全行を返す（削除フラグ付きを含む） */
function getAllTasks() {
  const sh = getSheet();
  if (sh.getLastRow() < 2) return [];
  const rows = sh.getDataRange().getValues();
  const hdrs = rows[0];
  const col = name => hdrs.indexOf(name);
  const out = [];
  for (let i = 1; i < rows.length; i++) {
    const r = rows[i];
    if (!r[col('タイトル')]) continue;
    const upd = r[col('更新日時')];
    out.push({
      id:         String(r[col('id')]       || ''),
      title:      String(r[col('タイトル')]  || ''),
      status:     String(r[col('状態')]      || '未着手'),
      priority:   String(r[col('優先度')]    || '中'),
      due:        toISO(r[col('期限')]),
      estimate:   String(r[col('見積もり')]  || ''),
      recurrence: String(r[col('繰り返し')]  || ''),
      note:       String(r[col('進捗メモ')]  || ''),
      tags:       String(r[col('タグ')]      || ''),
      deleted:    String(r[col('削除')]      || 'false'),
      updated:    upd ? new Date(upd).getTime() : 0
    });
  }
  return out;
}

/** 定期タスクシートの全行を返す */
function getAllRecurring() {
  const ss = SpreadsheetApp.openById(SPREADSHEET_ID);
  const sh = ss.getSheetByName(REC_SHEET);
  if (!sh || sh.getLastRow() < 2) return [];
  const rows = sh.getDataRange().getValues();
  const hdrs = rows[0];
  const col = name => hdrs.indexOf(name);
  const out = [];
  for (let i = 1; i < rows.length; i++) {
    const r = rows[i];
    if (!r[col('タイトル')]) continue;
    out.push({
      title:      String(r[col('タイトル')]  || ''),
      recurrence: String(r[col('繰り返し')]  || ''),
      next:       toISO(r[col('次回予定')]),
      priority:   String(r[col('優先度')]    || '中'),
      estimate:   String(r[col('見積もり')]  || '')
    });
  }
  return out;
}

function json(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}

/** シート取得（無ければ作成）＋ヘッダー整備 */
function getSheet() {
  const ss = SpreadsheetApp.openById(SPREADSHEET_ID);
  let sheet = ss.getSheetByName(SHEET_NAME);
  if (!sheet) sheet = ss.insertSheet(SHEET_NAME);
  sheet.getRange(1, 1, 1, HEADER.length).setValues([HEADER]);
  sheet.setFrozenRows(1);
  return sheet;
}

/** 定期タスクを recurring タブへ書き込む（毎回まるごと置き換え） */
function writeRecurring(list, now) {
  const ss = SpreadsheetApp.openById(SPREADSHEET_ID);
  let sh = ss.getSheetByName(REC_SHEET);
  if (!sh) sh = ss.insertSheet(REC_SHEET);
  sh.getRange(1, 1, 1, REC_HEADER.length).setValues([REC_HEADER]);
  sh.setFrozenRows(1);
  if (sh.getLastRow() > 1) {
    sh.getRange(2, 1, sh.getLastRow() - 1, REC_HEADER.length).clearContent();
  }
  const rows = list.map(r => [r.title, r.recurrence, r.next, r.priority, r.estimate, now]);
  if (rows.length) sh.getRange(2, 1, rows.length, REC_HEADER.length).setValues(rows);
}

/** 動作確認用：手動実行でシートを初期化 */
function setup() {
  getSheet();
  SpreadsheetApp.openById(SPREADSHEET_ID).toast('シート準備OK', '同期', 3);
}
