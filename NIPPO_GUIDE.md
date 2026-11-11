# 日報アプリ ガイドブック

日報アプリ（`nippo.html`）を、はじめて触る人向けに1枚でまとめたものです。

<!-- Web版 nippo-guide.html・PDF版 NIPPO_GUIDE.pdf はこのファイルから作っています:
     python3 tools/build_guide_html.py NIPPO_GUIDE.md nippo-guide.html --compact
     node tools/guide_pdf.js nippo-guide.html NIPPO_GUIDE.pdf -->

## 1. このアプリは何か

鰻のお店（屋台）で、**1日の営業の記録＝日報** をつけて、スタッフのLINEグループに送るためのWebアプリです。

| 項目 | 内容 |
|---|---|
| 使う人・端末 | お店のスタッフ。主にお店のiPad（スマホからも続きを書ける） |
| 公開URL | https://chenchengmkd-ctrl.github.io/task-app/nippo.html （GitHub Pages） |
| ファイル | `nippo.html` 1枚（HTML・CSS・JavaScript入り。ビルド不要）＋ サーバー側 `line-bot-vercel/`（Vercel） |

**できること**

- 会計ごとに **来店経緯**（インスタ・看板・リピーター…）・人数・食べたもの・お客さんの反応を記録
- **売上と注文明細はSquare（レジ）から自動**で入る／**常連さん台帳**（来店回数・よく頼むものが自動でたまる）
- **iPadとスマホで同じ日報を編集**／**ボタン1つでLINEグループへ送信**（勤怠Bot経由）

## 2. 1日の流れ

| いつ | 画面 | やること |
|---|---|---|
| 開店（1日1回） | ① 開始画面 | **記入者**（複数可）と **天気** を選んで「はじめる」 |
| 会計のたび | ② 会計を記録 | 人数を押す → 食べたものはSquareから自動 → **来店経緯を押す（必須）** → 任意で性別年代・お客さん・反応 → 「この会計を記録する」 |
| 閉店 | ③ 申し送り・実績 | 「営業おわり」→ 申し送り事項・インバウンドを入力。売上・累計はSquareから自動 |
| 最後 | LINEで送る | 確認して送信 → 翌日の開始画面へ（「取り消す」で戻れる） |

- 1組で来店経緯が人ごとに違うときは、別のボタンも押す（同じボタンをもう一度押すとその人数が1人増える。例：3名で インスタ1・看板2）。
- 「リピーター」を押すと常連さんを選ぶ画面が出る（新規登録もここで）。
- 間違えたら「↶ 元に戻す」／記録済みの組は「編集」から直せる／送る前に「プレビュー」で確認できる。
- 「設定」で **合言葉**（サーバーの `NIPPO_TOKEN`）を入れると、Square連携・端末間同期・Bot送信が有効になる。未入力だとその端末だけで動く。

## 3. 全体の仕組み

```
nippo.html（iPad／スマホ。日報はまず端末の localStorage に保存）
 ├─ ① 売上 ─→ task-app（Vercel） /api/nippo_sales ─→ Square
 ├─ ② 同期 ─→ task-app（Vercel） /api/nippo_sync  ─→ Supabase（bot_state）
 └─ ③ 送信 ─→ 勤怠Bot（Vercel・別リポジトリ） /api/nippo_post ─→ LINEグループ
```

| 通信先 | 役割 | コード |
|---|---|---|
| ① `/api/nippo_sales` | その日のSquare売上・月累計・会計明細を返す | `line-bot-vercel/linebot/nippo.py` |
| ② `/api/nippo_sync` | 日報・常連・設定を端末間でやりとり（新しい方が勝つ。中身のある日報は空の日報で上書きしない） | `line-bot-vercel/linebot/nippo_sync.py` |
| ③ `/api/nippo_post` | 日報文をLINEグループに投稿 | `line-kintai-vercel`（別リポジトリ）の `kintai/nippo_post.py` |

3つとも HTTPヘッダー `X-App-Token: <合言葉>` で認証します。保存場所は、日報が Supabase のキー `nippo:r:YYYY-MM-DD`、常連・名前・設定が `nippo:shared`（合言葉は端末ごと）。
