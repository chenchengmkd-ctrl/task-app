/**
 * LINE連携 — ①期限リマインド通知 ＋ ②チャットでタスク追加
 * Google Apps Script + LINE Messaging API
 *
 * ・アプリが同期するスプレッドシートと「同じ」スプレッドシートを使う
 *   - tasks       … ブラウザアプリが送信する表（こちらは読むだけ）
 *   - line_tasks  … LINEから追加したタスク（このBot専用。アプリ同期で消えない）
 * ・毎朝、アプリ分＋LINE追加分の「期限超過/今日/明日」をまとめてLINEに自動通知
 *
 * LINEトークでの使い方:
 *   牛乳を買う           → タスク追加
 *   牛乳を買う 6/25       → 期限つきで追加（末尾に M/D）
 *   一覧                 → LINE追加分の一覧
 *   完了 2 / 削除 2       → 操作
 *   通知                 → 今の期限リマインドを表示
 *   ヘルプ               → 使い方
 */

// ===== ① 自分の値を入れる =========================================
const CHANNEL_ACCESS_TOKEN = 'ここにチャネルアクセストークンを貼り付け';
const SPREADSHEET_ID       = 'アプリの送信先と同じスプレッドシートIDを貼り付け';
const SUPABASE_URL         = 'https://xldkfkhgazpugfuscpqt.supabase.co';
const SUPABASE_ANON_KEY    = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InhsZGtma2hnYXpwdWdmdXNjcHF0Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODM4NDg3MzgsImV4cCI6MjA5OTQyNDczOH0.C3_TYQI8R3HeXYWTzca9erUMjpTWm2sneB7hk5Bre8Y';
const APP_SHEET   = 'tasks';        // アプリが同期する表（読むだけ）
const LINE_SHEET  = 'line_tasks';   // LINEから追加した分
const REC_SHEET   = 'recurring';    // 定期タスク（アプリが同期）
const REMIND_HOUR   = 8;            // 毎朝の通知時刻（時・24時間制）
const REMIND_MINUTE = 0;            // 通知時刻（分）。nearMinuteで±15分ほどに絞る
const REC_LEAD_DAYS = 4;            // 定期タスクは「何日前」から通知するか
// =================================================================

const REPLY_URL = 'https://api.line.me/v2/bot/message/reply';
const PUSH_URL  = 'https://api.line.me/v2/bot/message/push';
const LINE_HEADER = ['id', 'userId', 'title', 'due', 'done', 'created'];

// 進捗状況の表示順とアイコン
const STATUS_ORDER = ['未着手', '着手中', '対応待ち', 'ペンディング'];
const STATUS_ICON  = { '未着手': '⚪', '着手中': '🔵', '対応待ち': '🟣', 'ペンディング': '🟡' };

/* ============ Webhook 入口 ============ */
function doPost(e) {
  try {
    const body = JSON.parse(e.postData.contents);
    (body.events || []).forEach(handleEvent);
  } catch (err) {
    console.error(err);
  }
  return ContentService.createTextOutput(JSON.stringify({ ok: true }))
    .setMimeType(ContentService.MimeType.JSON);
}

function handleEvent(ev) {
  if (ev.type !== 'message' || ev.message.type !== 'text') return;
  const userId = ev.source.userId;
  rememberUser(userId);                       // 通知用にユーザーを記憶
  const reply = routeCommand(userId, ev.message.text.trim());
  replyText(ev.replyToken, reply);
}

function routeCommand(userId, text) {
  const m = text.match(/^(完了|済|done|削除|消|delete)\s*(\d+)$/i);
  if (m) {
    const n = parseInt(m[2], 10);
    return /完了|済|done/i.test(m[1]) ? completeLine(userId, n) : deleteLine(userId, n);
  }
  if (/^(一覧|リスト|list)$/i.test(text)) return listAll(userId);
  if (/^(通知|リマインド|今日)$/i.test(text)) return buildReminder() || '📭 期限が近い（超過・今日・明日）のタスクはありません。';
  if (/^(ヘルプ|使い方|help)$/i.test(text)) return helpText();
  if (!text) return '空メッセージです。「ヘルプ」で使い方を表示します。';
  return addLine(userId, text);
}

/* ============ line_tasks の操作 ============ */
function lineSheet() {
  const ss = SpreadsheetApp.openById(SPREADSHEET_ID);
  let sh = ss.getSheetByName(LINE_SHEET);
  if (!sh) sh = ss.insertSheet(LINE_SHEET);
  if (sh.getLastRow() === 0) sh.appendRow(LINE_HEADER);
  return sh;
}
function addLine(userId, text) {
  let title = text, due = '', projName = '';
  // [プロジェクト名] タスク名 の形式を検出（タイトルには[]をそのまま残す）
  const projMatch = title.match(/^\[(.+?)\]/);
  if (projMatch) projName = projMatch[1];
  // 末尾の「M/D」を期限として取り出す
  const dm = title.match(/^(.*?)\s+(\d{1,2})\/(\d{1,2})$/);
  if (dm) { title = dm[1].trim(); due = toISOThisYear(dm[2], dm[3]); }

  lineSheet().appendRow([
    Date.now().toString(36) + Math.random().toString(36).slice(2, 6),
    userId, title, due, false, new Date().toISOString()
  ]);
  const cnt = getLineTasks(userId, true).length;
  let msg = `✅ 追加しました\n「${projName ? title.replace(/^\[.+?\]\s*/, '') : title}」`;
  if (projName) msg += `\n📁 ${projName}`;
  if (due) msg += `\n📅 ${jp(due)}`;
  msg += `\n\n未完了: ${cnt}件`;
  return msg;
}
function getLineTasks(userId, onlyActive) {
  const rows = lineSheet().getDataRange().getValues();
  const out = [];
  for (let i = 1; i < rows.length; i++) {
    const r = rows[i];
    if (r[1] !== userId) continue;
    const done = r[4] === true || r[4] === 'TRUE' || r[4] === 'true';
    if (onlyActive && done) continue;
    out.push({ row: i + 1, title: r[2], due: r[3], done });
  }
  return out;
}
// アプリ＋定期＋LINEの未完了を「進捗状況ごと」に表示
function listAll(userId) {
  const app = getAppTasksAll();
  const rec = getRecurring();
  const line = getLineTasks(userId, true);
  if (!app.length && !rec.length && !line.length) return '🎉 未完了のタスクはありません。';

  let msg = '📋 タスク一覧\n';

  // 進捗状況ごとにグループ表示
  STATUS_ORDER.forEach(st => {
    const arr = app.filter(t => t.status === st);
    if (!arr.length) return;
    msg += '\n' + STATUS_ICON[st] + ' ' + st + '（' + arr.length + '）\n' +
      arr.map(t => '・' + t.title + (t.due ? '（' + jp(t.due) + '）' : '')).join('\n') + '\n';
  });

  // 定期タスク
  if (rec.length) {
    msg += '\n🔁 定期タスク（' + rec.length + '）\n' +
      rec.map(r => '・' + r.title + '（' + r.recurrence + (r.next ? '・次回 ' + jp2(r.next) : '') + '）').join('\n') + '\n';
  }

  // LINE受信箱（まだアプリに取り込まれていない分）
  if (line.length) {
    msg += '\n💬 LINE受信箱（' + line.length + '・アプリ未取込）\n' +
      line.map((t, i) => `${i + 1}. ${t.title}${t.due ? '（' + jp(t.due) + '）' : ''}`).join('\n') + '\n';
    msg += '\n「完了 番号」「削除 番号」はLINE受信箱の番号に使えます。';
  }
  return msg.trim();
}
// アプリの表（tasks）から未完了タスクを全部取得（状態つき・期限の有無は問わない）
function getAppTasksAll() {
  const sh = SpreadsheetApp.openById(SPREADSHEET_ID).getSheetByName(APP_SHEET);
  if (!sh || sh.getLastRow() < 2) return [];
  const rows = sh.getDataRange().getValues();   // 列: タイトル, 状態, 優先度, 期限, ...
  const out = [];
  for (let i = 1; i < rows.length; i++) {
    const r = rows[i];
    if (!r[0] || r[1] === '完了') continue;
    out.push({ title: String(r[0]), due: r[3] || '', status: String(r[1] || '未着手') });
  }
  return out;
}
// 定期タスク（recurring タブ）を取得
function getRecurring() {
  const sh = SpreadsheetApp.openById(SPREADSHEET_ID).getSheetByName(REC_SHEET);
  if (!sh || sh.getLastRow() < 2) return [];
  const rows = sh.getDataRange().getValues();   // 列: タイトル, 繰り返し, 次回予定, 優先度, ...
  const out = [];
  for (let i = 1; i < rows.length; i++) {
    const r = rows[i];
    if (!r[0]) continue;
    out.push({ title: String(r[0]), recurrence: String(r[1] || ''), next: parseDate(r[2]) });
  }
  return out;
}
function completeLine(userId, n) {
  const ts = getLineTasks(userId, true);
  if (n < 1 || n > ts.length) return `番号 ${n} が見つかりません。「一覧」で確認してください。`;
  lineSheet().getRange(ts[n - 1].row, 5).setValue(true);
  return `✔️ 完了：「${ts[n - 1].title}」`;
}
function deleteLine(userId, n) {
  const ts = getLineTasks(userId, true);
  if (n < 1 || n > ts.length) return `番号 ${n} が見つかりません。「一覧」で確認してください。`;
  const t = ts[n - 1];
  lineSheet().deleteRow(t.row);
  return `🗑️ 削除：「${t.title}」`;
}

/* ============ リマインド ============ */
// アプリの表（tasks）から期限ありの未完了を取得
function getDueFromApp() {
  const sh = SpreadsheetApp.openById(SPREADSHEET_ID).getSheetByName(APP_SHEET);
  if (!sh) return [];
  const rows = sh.getDataRange().getValues();
  const out = [];
  // 列: タイトル, 状態, 優先度, 期限, ...
  for (let i = 1; i < rows.length; i++) {
    const r = rows[i];
    const title = r[0], status = r[1], due = parseDate(r[3]);
    if (!title || !due || status === '完了') continue;
    out.push({ title: String(title), due, status: String(status || '未着手') });
  }
  return out;
}
// LINE追加分から期限ありの未完了を取得
function getDueFromLine() {
  const rows = lineSheet().getDataRange().getValues();
  const out = [];
  for (let i = 1; i < rows.length; i++) {
    const r = rows[i];
    const done = r[4] === true || r[4] === 'TRUE' || r[4] === 'true';
    const due = parseDate(r[3]);
    if (done || !due) continue;
    out.push({ title: String(r[2]), due });
  }
  return out;
}
// 通知メッセージを組み立て（対象が無ければ空文字）
// ・通常タスク: 期限が「超過/今日/明日」を、進捗状況ごとに表示
// ・定期タスク: 次回予定が REC_LEAD_DAYS 日以内を表示
function buildReminder() {
  const today = midnight(new Date());

  // 期限が近い通常タスク（状態つき）を集める
  const near = [];
  getDueFromApp().forEach(t => {
    const d = dayDiff(today, t.due);
    if (d <= 1) near.push({ title: t.title, due: t.due, status: t.status, d });
  });
  getDueFromLine().forEach(t => {
    const d = dayDiff(today, t.due);
    if (d <= 1) near.push({ title: t.title, due: t.due, status: '未着手', line: true, d });
  });

  // もうすぐの定期タスク（3〜4日前から）
  const recSoon = getRecurring().filter(r => {
    if (!r.next) return false;
    const d = dayDiff(today, r.next);
    return d >= 0 && d <= REC_LEAD_DAYS;
  }).sort((a, b) => dayDiff(today, a.next) - dayDiff(today, b.next));

  if (!near.length && !recSoon.length) return '';

  let msg = `🔔 タスクのリマインド (${today.getMonth() + 1}/${today.getDate()})\n`;

  // 進捗状況ごとに表示（各行に期日も）
  STATUS_ORDER.forEach(st => {
    const arr = near.filter(t => t.status === st).sort((a, b) => a.d - b.d);
    if (!arr.length) return;
    msg += '\n' + STATUS_ICON[st] + ' ' + st + '\n' +
      arr.map(t => '・' + t.title + '（' + dueTag(t.d, t.due) + '）' + (t.line ? ' 💬' : '')).join('\n') + '\n';
  });

  // 定期タスク
  if (recSoon.length) {
    msg += '\n🔁 定期タスク（' + REC_LEAD_DAYS + '日以内）\n' +
      recSoon.map(r => '・' + r.title + '（' + r.recurrence + '・' + dueTag(dayDiff(today, r.next), r.next) + '）').join('\n') + '\n';
  }
  return msg.trim();
}
// 今日との日数差（0=今日, 1=明日, -1=昨日）
function dayDiff(today, dateObj) {
  return Math.round((midnight(dateObj) - today) / 86400000);
}
// 期日を「今日/明日/N日後/N日超過 M/D」の形に
function dueTag(d, dateObj) {
  const md = (dateObj.getMonth() + 1) + '/' + dateObj.getDate();
  if (d < 0) return '⚠' + (-d) + '日超過 ' + md;
  if (d === 0) return '今日 ' + md;
  if (d === 1) return '明日 ' + md;
  return d + '日後 ' + md;
}
// 毎朝のトリガーから呼ばれる
function sendReminders() {
  const msg = buildReminder();
  if (!msg) return;
  getUsers().forEach(uid => pushText(uid, msg));
}

/* ============ ユーザー記憶（通知先） ============ */
function rememberUser(userId) {
  if (!userId) return;
  const p = PropertiesService.getScriptProperties();
  let ids = [];
  try { ids = JSON.parse(p.getProperty('USER_IDS') || '[]'); } catch (e) {}
  if (ids.indexOf(userId) < 0) { ids.push(userId); p.setProperty('USER_IDS', JSON.stringify(ids)); }
}
function getUsers() {
  try { return JSON.parse(PropertiesService.getScriptProperties().getProperty('USER_IDS') || '[]'); }
  catch (e) { return []; }
}

/* ============ LINE送信 ============ */
function replyText(token, text) {
  UrlFetchApp.fetch(REPLY_URL, {
    method: 'post', contentType: 'application/json',
    headers: { Authorization: 'Bearer ' + CHANNEL_ACCESS_TOKEN },
    payload: JSON.stringify({ replyToken: token, messages: [{ type: 'text', text: text }] }),
    muteHttpExceptions: true
  });
}
function pushText(to, text) {
  UrlFetchApp.fetch(PUSH_URL, {
    method: 'post', contentType: 'application/json',
    headers: { Authorization: 'Bearer ' + CHANNEL_ACCESS_TOKEN },
    payload: JSON.stringify({ to: to, messages: [{ type: 'text', text: text }] }),
    muteHttpExceptions: true
  });
}

/* ============ 補助 ============ */
function helpText() {
  return [
    '📖 使い方',
    '',
    '・文章を送る → タスク追加',
    '　例）牛乳を買う',
    '　例）牛乳を買う 6/25  ← 末尾に日付で期限つき',
    '・一覧 → アプリ＋LINEの未完了をまとめて表示',
    '・完了 2 / 削除 2 → 操作',
    '・通知 → 今の期限リマインドを表示',
    '',
    `毎朝${REMIND_HOUR}時に、アプリの分も含めて期限を自動通知します。`
  ].join('\n');
}
function parseDate(v) {
  if (!v) return null;
  if (Object.prototype.toString.call(v) === '[object Date]') return midnight(v);
  const m = String(v).trim().match(/^(\d{4})[-\/](\d{1,2})[-\/](\d{1,2})/);
  return m ? new Date(+m[1], +m[2] - 1, +m[3]) : null;
}
function midnight(d) { return new Date(d.getFullYear(), d.getMonth(), d.getDate()); }
function toISOThisYear(mm, dd) {
  const y = new Date().getFullYear(), p = n => String(n).padStart(2, '0');
  return y + '-' + p(mm) + '-' + p(dd);
}
function jp(iso) { const d = parseDate(iso); return d ? (d.getMonth() + 1) + '/' + d.getDate() : iso; }
function jp2(d) { return (d.getMonth() + 1) + '/' + d.getDate(); }

/* ============ Supabase ヘルパー ============ */
function getSupabase(table, params) {
  const url = SUPABASE_URL + '/rest/v1/' + table + '?' + params;
  const res = UrlFetchApp.fetch(url, {
    headers: {
      'apikey': SUPABASE_ANON_KEY,
      'Authorization': 'Bearer ' + SUPABASE_ANON_KEY,
      'Accept': 'application/json'
    },
    muteHttpExceptions: true
  });
  if (res.getResponseCode() !== 200) {
    console.error('Supabase error', res.getResponseCode(), res.getContentText());
    return [];
  }
  return JSON.parse(res.getContentText());
}

/* ============ 週次レポート ============ */
function sendWeeklyReport() {
  const today = new Date();
  const weekAgo = new Date(today);
  weekAgo.setDate(weekAgo.getDate() - 7);
  const since = weekAgo.toISOString().slice(0, 10);

  // 先週完了したタスク（updated_at >= 7日前 AND done=true）
  const doneTasks = getSupabase('tasks',
    'done=eq.true&deleted=eq.false&updated_at=gte.' + since + '&select=title,project_id,updated_at&order=updated_at.desc');

  // 先週の日次ログ
  const logs = getSupabase('daily_logs',
    'log_date=gte.' + since + '&select=log_date,content&order=log_date.desc');

  // プロジェクト名マップ
  const projects = getSupabase('projects', 'select=id,name');
  const projMap = {};
  projects.forEach(p => { projMap[p.id] = p.name; });

  const m = today.getMonth() + 1, d = today.getDate();
  let msg = `📊 週次レポート（${m}/${d}）\n`;

  // 完了タスク
  msg += `\n✅ 先週の完了タスク（${doneTasks.length}件）`;
  if (doneTasks.length) {
    msg += '\n' + doneTasks.map(t => {
      const proj = t.project_id && projMap[t.project_id] ? '📁' + projMap[t.project_id] + ' ' : '';
      return '・' + proj + t.title;
    }).join('\n');
  } else {
    msg += '\nなし';
  }

  // 日次ログ
  msg += `\n\n📓 振り返りログ（${logs.length}件）`;
  if (logs.length) {
    msg += '\n' + logs.map(l => {
      const ld = new Date(l.log_date);
      const label = (ld.getMonth() + 1) + '/' + ld.getDate();
      const excerpt = l.content ? l.content.slice(0, 60).replace(/\n/g, ' ') + (l.content.length > 60 ? '…' : '') : '（内容なし）';
      return label + '：' + excerpt;
    }).join('\n');
  } else {
    msg += '\nなし';
  }

  if (doneTasks.length === 0 && logs.length === 0) {
    msg += '\n\n今週もお疲れ様でした！来週も頑張りましょう💪';
  } else {
    msg += '\n\n今週もお疲れ様でした！';
  }

  getUsers().forEach(uid => pushText(uid, msg));
}

/* ============ セットアップ（1回だけ実行）============ */
// 毎朝の自動通知トリガーを作成
function installDailyTrigger() {
  ScriptApp.getProjectTriggers().forEach(t => {
    if (t.getHandlerFunction() === 'sendReminders') ScriptApp.deleteTrigger(t);
  });
  ScriptApp.newTrigger('sendReminders').timeBased()
    .atHour(REMIND_HOUR).nearMinute(REMIND_MINUTE).everyDays(1).create();
}
// 毎週月曜 9時に週次レポートを送るトリガーを作成
function installWeeklyTrigger() {
  ScriptApp.getProjectTriggers().forEach(t => {
    if (t.getHandlerFunction() === 'sendWeeklyReport') ScriptApp.deleteTrigger(t);
  });
  ScriptApp.newTrigger('sendWeeklyReport').timeBased()
    .onWeekDay(ScriptApp.WeekDay.MONDAY).atHour(9).create();
}
// テスト：今すぐ自分に通知を送る（先にLINEから1回メッセージを送っておくこと）
function testReminder() {
  if (!getUsers().length) { Logger.log('ユーザー未登録：先にLINEからBotへメッセージを送ってください。'); return; }
  sendReminders();
  Logger.log('送信しました（対象があれば届きます）');
}
// テスト：今すぐ週次レポートを送る
function testWeeklyReport() {
  if (!getUsers().length) { Logger.log('ユーザー未登録：先にLINEからBotへメッセージを送ってください。'); return; }
  sendWeeklyReport();
  Logger.log('週次レポートを送信しました');
}
