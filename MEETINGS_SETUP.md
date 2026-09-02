# 議事録（meetings.html）セットアップ

PCの議事録ツールで文字起こしした会議を、議事録にしてスマホから読めるようにします。
既存の「タスク管理アプリ」と同じ GitHub Pages・同じ Supabase・同じ Vercel をそのまま使います。

**音声・動画はPCから出ません。** 送るのは文字起こしのテキストだけで、
サーバーはそれを Gemini に通して議事録にしたあと、**議事録とアクションだけ**を保存します。
生の発言はクラウドには残りません（PCの `.txt` に残ります）。

追加費用はありません。Gemini は既存の無料枠、Supabase と Vercel も今の無料プランのままです。

あなたの作業は **STEP 1 と STEP 2** だけです。

---

## STEP 1. Supabase にテーブルを作る（1回だけ）

1. [Supabase](https://supabase.com/dashboard) を開き、今使っているプロジェクト（`xldkfkhgazpugfuscpqt`）を選ぶ
2. 左メニューの **SQL Editor**（`</>` のアイコン）を開く
3. **New query** を押し、下をまるごと貼り付けて **RUN**（右下の緑ボタン）を押す

```sql
create table if not exists meetings (
  id text primary key,
  title text not null,
  meeting_date date,
  duration_sec int,
  minutes_md text default '',
  actions jsonb default '[]'::jsonb,
  source_file text unique,
  created_at timestamptz default now(),
  updated_at timestamptz default now()
);
create index if not exists idx_meetings_date on meetings(meeting_date desc);

-- 他のテーブルと同じ運用に揃える（これが無いと画面が「permission denied」になる）
alter table meetings disable row level security;
```

4. 「Success. No rows returned」と出れば完了です。

---

## STEP 2. Vercel に合言葉を登録する（1回だけ）

この口はPCから書き込みができるので、他人に叩かれないよう合言葉を必須にしています。
**未登録だと動きません**（誰でも書き込める状態で公開しないため、わざとそうしています）。

1. [Vercel](https://vercel.com/dashboard) を開き、このプロジェクトを選ぶ
2. **Settings → Environment Variables**
3. 次を追加する
   - Name: `MEETING_TOKEN`
   - Value: 長いランダムな文字列（Claudeが作ったものをそのまま使ってください）
4. **Save** を押す
5. **Deployments** タブ → 一番上の行の右の「…」→ **Redeploy**
   （環境変数は再デプロイしないと反映されません）

同じ文字列を、PCの議事録ツールの画面（録画タブの「クラウドの合言葉」）にも入れて「保存」を押します。

> 合言葉はこのファイルには書きません。GitHub に公開されてしまうためです。

---

## STEP 3. 公開する

このリポジトリに push すると自動で公開されます。

- `meetings.html` … GitHub Pages → **https://chenchengmkd-ctrl.github.io/task-app/meetings.html**
- `line-bot-vercel/` … Vercel が自動デプロイ

反映まで1〜2分。スマホでこのURLをブックマークしてください。

---

## 使い方

やり方は2つあります。**大事な会議は A**、ざっと残すだけなら B。

### A. Claudeに作らせる（品質が一番よい）

1. PCで議事録ツールを開く（`python meet_app.py`）
2. 録画の **「文字起こし」** を押して本文を開く
3. **「Claudeに貼って作る（無料）」** → claude.ai が開くので Ctrl+V して Enter
4. 出てきた議事録をコピーし、PCの画面の下の欄に貼って **「この議事録を保存する」**
5. スマホで `meetings.html` を開く

議事録は `_議事録.md` としてPCにも残ります。この経路では文字起こし本文はサーバーに送りません。

### B. サーバーに任せる（押すだけ）

1. 録画の **「クラウドに送る」** を押す。1〜2分で議事録ができます
2. スマホで `meetings.html` を開く

こちらは無料枠の軽いモデル（Gemini）が作るため、Aより浅くなります。
議題のまとめ方が粗く、指摘の中身が薄くなる傾向があります。

| タブ | できること |
|---|---|
| **会議** | 会議ごとに議事録を開く。アクション表・議題・録画で確認すべき箇所の3部構成 |
| **アクション** | 全会議を横断して、期限が近い順に並べる。期限切れは赤、3日以内は黄 |
| **検索** | 過去の議事録を横断して探す（スペース区切りでAND） |

同じ録画をもう一度送ると、議事録は**作り直し**になります（内容が気に入らないときに使えます）。

---

## 注意点

- 文字起こしは固有名詞・金額をよく間違えます。議事録には「要確認」が付きますが、
  **金額は必ず録画で確認してください**。数字が並ぶ場面はほぼ信用できません。
- アクションの担当・期限は、文字起こしから読み取れないと「不明」になります。
  推測で埋めない方針です。
- 生の文字起こしはクラウドにありません。発言そのものを確認したいときは、
  PCの議事録ツールの「検索」タブを使ってください。
