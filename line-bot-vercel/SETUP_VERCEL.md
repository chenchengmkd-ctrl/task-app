# LINEボットの移行セットアップ（GAS → Vercel/Python）

これまでの Google Apps Script 版（`line-bot/Code.gs`）と同じ機能を、Vercel（無料・カード登録不要）＋Pythonで動かすための手順です。今後は「ファイルを直して git push するだけ」で本番に反映されるようになります（GASの「貼り替え→保存→新バージョンでデプロイ」は不要になります）。

作業は一度だけです。順番に進めてください。詰まったら、その画面のスクリーンショットを送ってもらえれば案内します。

---

## STEP A. Vercelアカウントを作る

1. https://vercel.com/signup を開く
2. 「Continue with GitHub」を選ぶ（普段使っているGitHubアカウントでログインするだけ。クレジットカードの登録は不要）
3. 個人利用（Hobby）プランのまま進める

---

## STEP B. リポジトリをVercelにインポートする

1. Vercelのダッシュボードで「Add New...」→「Project」
2. `task-app` リポジトリを選んで「Import」
3. 「Root Directory」という項目を **`line-bot-vercel`** に設定する（これが超重要：ここを指定しないと、Webアプリ側のファイルまで巻き込んでビルドしようとして失敗します）
4. 「Framework Preset」は特に選ばなくてOK（Other のままでよい）
5. まだ「Deploy」は押さずに、次のSTEP Cで環境変数を先に入れる

---

## STEP C. 環境変数を設定する

同じ画面の「Environment Variables」欄で、以下を1つずつ「Name」と「Value」に入力して追加してください（全部で8個）。

| Name | Value（値） |
|---|---|
| `CHANNEL_ACCESS_TOKEN` | 今のLINEのチャネルアクセストークン（GASのCode.gs 19行目にあったのと同じ値） |
| `SUPABASE_URL` | `https://xldkfkhgazpugfuscpqt.supabase.co`（今と同じ） |
| `SUPABASE_ANON_KEY` | 今のSupabase anonキー（Code.gs 21行目と同じ値） |
| `GEMINI_API_KEY` | 今のGemini APIキー（Code.gs 22行目と同じ値） |
| `GEMINI_MODEL` | `gemini-3.1-flash-lite` |
| `CRON_SECRET` | 自分で決める適当な長いランダム文字列（例：パスワード生成サイトで作った32文字くらいの文字列。メモしておく） |
| `POLL_SECRET` | `CRON_SECRET`とは別の、もう1つのランダム文字列（同様にメモしておく） |
| `GITHUB_PAT` | STEP Fで作るGitHubのトークン（後で追加でもOK。先に空欄のまま進めて、STEP Fの後で追加してもよい） |
| `GITHUB_REPO` | `chenchengmkd-ctrl/task-app` |

入力し終わったら「Deploy」を押す。1〜2分でデプロイが完了し、`https://（何か）.vercel.app` のようなURLが発行されます。**このURLを控えておいてください。**

---

## STEP D. LINEのWebhook URLを新しいものに変更する

1. [LINE Developers コンソール](https://developers.line.biz/console/) を開く
2. 今使っているチャネルを選ぶ
3. 「Messaging API設定」タブ →「Webhook URL」を編集
4. STEP Cで発行されたURLの末尾に `/api/webhook` を付けたものを入力
   - 例：`https://task-app-xxxx.vercel.app/api/webhook`
5. 「検証」ボタンを押して成功（Success）することを確認
6. 「Webhookの利用」がオンになっていることを確認

---

## STEP E. Supabaseに `bot_state` テーブルを作る

GASの「保留中の返信」を覚えておく仕組み（PropertiesService）の代わりに使うテーブルです。SupabaseのSQL Editorで以下を実行してください。

```sql
CREATE TABLE IF NOT EXISTS bot_state (
  key text primary key,
  value jsonb,
  updated_at timestamptz default now()
);
```

---

## STEP F. GitHub Actionsで5分おきのチェックを有効にする

これは既にリポジトリに `.github/workflows/line-bot-poll.yml` として用意済みです。あなたがやることは、呼び出し先のURL（秘密の鍵つき）を1つ、GitHubのシークレットとして登録するだけです。

1. GitHubで `task-app` リポジトリを開く
2. 「Settings」タブ →左側「Secrets and variables」→「Actions」
3. 「New repository secret」をクリック
4. Name: `LINE_BOT_POLL_URL`
5. Value: `https://（STEP Cで控えたURL）/api/cron_poll?key=（STEP Cで決めたPOLL_SECRETの値）`
   - 例：`https://task-app-xxxx.vercel.app/api/cron_poll?key=abc123...`
6. 「Add secret」で保存

これで5分おきに自動で実行されるようになります（「Actions」タブでいつでも実行状況を確認できます）。

### GitHubのPersonal Access Token（STEP CのGITHUB_PAT）の作り方
週次レポートのときに、GitHub Actionsが長期間止まらないよう自動で小さなコミットを打つために使います。

1. GitHubの右上アイコン →「Settings」→左メニュー一番下「Developer settings」
2. 「Personal access tokens」→「Fine-grained tokens」→「Generate new token」
3. Repository access: 「Only select repositories」→`task-app`を選択
4. Permissions →「Repository permissions」→「Contents」を「Read and write」に設定
5. 「Generate token」を押し、表示されたトークン（`github_pat_...`）をコピー
6. Vercelの環境変数 `GITHUB_PAT` にこの値を貼り付け、再デプロイ（Vercelのプロジェクト画面から「Redeploy」）

---

## STEP G. 並行稼働で確認する

GAS側（`line-bot/Code.gs`）は**そのまま止めずに**残しておいて構いません。しばらく両方が動く状態になりますが、実際のデータはSupabaseを共有しているので、機能がぶつかることはありません（通知が二重に届く可能性はあるので、確認後にGAS側のトリガーだけ止めます）。

1. LINEで「一覧」「牛乳を買う」「相談」「毎週月曜19時に〇〇を定期タスクにして」「振り返り：テスト」など、一通り試す
2. 期待通りの返信が来るか確認する
3. 問題が無ければ、Google Apps Scriptの編集画面で「トリガー」（時計アイコン）を開き、すべてのトリガーを削除する（GASのコード自体は参考用に残しておいてよい）

---

## うまくいかない時

| 症状 | 確認 |
|---|---|
| LINEからの返信が来ない | Vercelの「Deployments」タブでビルドが成功しているか／LINE Developersの「検証」がSuccessか |
| Webhookの検証が失敗する | Root Directoryが`line-bot-vercel`になっているか／URLの末尾が`/api/webhook`か |
| 通知だけ来ない（返信はできる） | Vercelの「Cron Jobs」タブで4つとも登録されているか／GitHub Actionsの「Actions」タブで5分おきの実行が成功しているか |
| 401 Unauthorizedのようなエラーがログに出る | `CRON_SECRET`／`POLL_SECRET`の値がVercelとGitHub Actionsのシークレットで一致しているか |
| 週次レポートは届くがハートビートコミットが無い | `GITHUB_PAT`と`GITHUB_REPO`が正しく設定されているか／トークンの権限が「Contents: Read and write」になっているか |
| AIコーチが反応しない | `GEMINI_API_KEY`が正しいか／Vercelの環境変数を追加した後に「Redeploy」したか（環境変数は追加しただけでは反映されず、再デプロイが必要です） |

## コードを更新したら
このリポジトリに git push するだけで、Vercelが自動でビルド・デプロイします（数十秒〜1分程度）。GASのような「貼り替え・保存・新バージョンでデプロイ」の手作業は一切不要です。
