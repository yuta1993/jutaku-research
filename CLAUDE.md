# うちなーらいふ 新着物件通知システム

## 目的

沖縄の不動産ポータル「うちなーらいふ」(https://www.e-uchina.net) を定期的に巡回し、
**指定した地域・地区に新しく掲載された物件（＝物件IDが新規のもの）** を検出して、
その物件の詳細リンクをメールで通知する。

- 「更新」（価格改定など、物件IDが変わらない変化）は通知しない。**ID新規のみ**が対象。
- ローカルPCで動かす想定。Windowsタスクスケジューラ等で定期的にバッチ実行する。
  PCが起動していない時間帯は動かない（取りこぼし対策はタスクスケジューラ設定で吸収）。

## 動作概要（1回の実行フロー）

```
① 【その日の初回実行なら】robots.txt 確認 ＋ 古いスナップショット削除
     ・data/snapshots/ に「今日の日付のファイルが無い」= 本日初回と判定
     ・robots.txt を取得し、使用パス（/api/配下）が許可されているか確認
        - 取得・解析できない → 判断不能として通常実行を続行（警告ログのみ）
        - 明確に禁止 → 収集・通知を中止し、停止理由をアラートメールで通知（1日1通）
     ・スナップショットは直近 20 件（=直近20回ぶん）を残し、それ以前を削除
        - 比較基準（③）を確定した後に削除する（比較対象を消さないため）
        - 件数ベースなので、何日も実行しない期間があっても履歴が消えすぎない
        ▼
② 設定ファイルの全地域・全地区を巡回し、JSON APIから物件ID(と詳細URL)をかき集める
     ・地域(市町村)間・ページ間のアクセスには間隔を空ける（既定1.5秒・礼儀/負荷配慮）
        ▼
③ 保存する前に「比較基準」を作る = 「当日 ＋ 当日より前で最も新しいデータ日」の全ファイルのID和集合
     ・当日2回目以降 → 当日分すべて ＋ 直近の過去データ日分すべて の和集合
     ・当日初回       → 直近の過去データ日分すべて（カレンダー上何日前でも“その1日”）の和集合
     ・「直近の過去データ日」はファイルがある最新の過去日。PCを数日止めて直近が3日前でも、その日を1つ拾う（日数は固定しない）
     ・取りこぼし（収集中のページずれ等）で一時的にIDが欠けても、当日内・前データ日の他ファイルが補完する
        ▼
④ 今回集めたIDを新しいタイムスタンプ名で保存（= 実行ごとにファイルが1つ増える）
        ▼
⑤ 差分 = 今回のID − 比較基準のID和集合（= 新しく現れたID）
     ・消えた物件（前回あって今回無い）は無視。新着通知が目的のため。
        ▼
⑥ 新着があれば物件の詳細リンク(permalink)をメール送信。
   新着が無くても「新着物件なし」通知を送る（ベースライン作成時=初回/新規追加対象は送らない）
```

## サイト構造とデータ取得方式（調査・実装で確定）

うちなーらいふは **Vue.js製のSPA** で、物件一覧カードはJavaScriptが内部JSON APIから取得して
描画している。**サーバが返す生HTMLに物件IDは含まれない**。
そのため当初想定の「HTMLをパース」ではなく、**内部JSON APIを直接叩く**方式で実装した。
→ 軽量（`requests`のみ）で実現でき、ヘッドレスブラウザ(Playwright等)は **不要**。

### 人が見るページURL（slugを控える用）
- 一覧（市町村）: `https://www.e-uchina.net/house/{市町村slug}` 例 `/house/okinawashi`
- 一覧（市町村+地区）: `https://www.e-uchina.net/house/{市町村slug}/{地区slug}` 例 `/house/okinawashi/noborikawa`
- 詳細: `https://www.e-uchina.net/bukken/house/{物件ID}/detail.html`
- 市町村・地区は **ローマ字slug**（沖縄市→`okinawashi`、登川→`noborikawa`）。
  slugはサイトで対象地域を開いてアドレスバーURL末尾を控える運用。

### 内部JSON API（実装で使用）
ベース: `https://www.e-uchina.net/api`　ヘッダ: `Accept: application/json`（+ UA）

| 用途 | リクエスト | 返却 |
|---|---|---|
| slug→city_code | `GET /api/area/get_searchable_cities?filter=` | `{data:[{slug, city_code, city_name}]}` |
| 地区slug→area_code | `GET /api/area/get_searchable_areas?city_code={code}` | `{data:[{slug, area_code, area_name}]}` |
| **物件検索** | `GET /api/search?searchType={type}&city={city_code}&areas={area_code,...}&page={n}` | 下記 |

- `searchType` は物件種別slug（`house` / `mansion` / `tochi` …）。
- `areas` は省略可（省略＝市町村全体）。複数地区はカンマ区切り。
- 物件検索レスポンス: `{"status":"ok","data":{"bukkens":{"current_page","last_page","per_page","total","data":[ 物件… ]}}}`
- 物件オブジェクトの利用フィールド:
  - `id` … 安定した数値の一意ID（**新着判定のキー**）例 `5530594`
  - `permalink` … 詳細ページURL（**メールに載せるリンク**）例 `https://www.e-uchina.net/bukken/house/h-6599-7260616-0389/detail.html`
  - `catch_phrase_web` … キャッチコピー（メールのタイトル表示に流用、無ければ空）
- ページングは `page` を増やし、`last_page` に達したら終了。1ページ20件。

> 注: `fudosan_search` は**不動産会社の検索**で物件一覧ではない（混同しないこと）。
> 物件一覧は `search`（GET専用。POSTは405）。`type=` ではなく `searchType=` が正しいパラメータ名。

### robots / 礼儀
- robots.txt の禁止は `/net21/`（掲載店用管理画面）のみ。`/house/`・`/bukken/`・`/api/` は対象外。
- **本日初回実行時に robots.txt を自動確認する**（`check_robots`）。使用する `/api/` 配下が
  禁止に変わった場合は収集・通知を中止し、停止理由をアラートメールで通知する（フェイルセーフ）。
  - 取得・解析に失敗した場合は判断不能として通常実行を続行（robots取得失敗だけでは止めない）。
  - アラートメールは受信箱の氾濫を避けるため **1日1通**に制限（`data/snapshots/.robots_alert_last` で管理）。
- 利用規約（`/cms/term-of-use/`）は「過度な負荷」「営利目的利用」を禁止。個人利用・低頻度なら問題になりにくいが、
  集めた情報の再配布・商用利用は不可。内部APIは非公開仕様のため将来変わりうる。
  アクセスは1日数回・リクエスト間に間隔（既定1.5秒）に抑える。

## 設定ファイル `config/watchlist.yaml`

編集するのは基本このファイルだけ。**物件種別(search_type)ごとに監視する地域(市町村)の配列を持ち、各地域に複数の地区(配列)** を持たせる。
これにより「住宅は南風原だけ／土地は南風原・豊見城・与那原」のように種別ごとに地域を変えられる。
地区を書かない（`areas: []` または省略）場合は、その地域全体を監視する。

主なキー:
- `http` … `user_agent` / `request_interval_sec`(既定1.5) / `timeout_sec`(既定30) / `max_pages`(既定60)
- `mail` … `smtp_host`/`smtp_port`/`user`/`from_addr`/`password_file`（無ければ`password_env`）/`default_to[]`/`detail_fields[]`
  - `detail_fields` … メールで各物件のタイトル下に出す項目（上から順）。指定可能キーと対応APIフィールドは `watch.py` の `ITEM_DETAIL_CATALOG` を参照:
    `price`(価格)/`madori`(間取り)/`land`(土地面積)/`building`(建物面積)/`built`(築年)/`address`(住所)/`parking`(駐車場)/`transport`(交通)/`madori_detail`(間取り詳細)
- `watches[]` … 物件種別ごとの監視ブロックの配列。各ブロック:
  - `search_type` … 物件種別（`house`/`mansion`/`tochi`）
  - `regions[]` … その種別で監視する地域。各region: `label` / `city_slug` / `areas[]`（省略=全域）/ `to[]`（任意・default_toを上書き）
  - （後方互換）旧形式のトップレベル `search_type` + `regions[]` も読める。`search_type` がリストならその全種別に同じ `regions` を割り当てる。

内部展開（各監視単位＝種別×地域 ごとにAPIを叩く）:
- house × 沖縄市 + [noborikawa] → city_code=47211, area_code=9042142 で `GET /api/search?searchType=house&city=47211&areas=9042142&page=N`
- tochi × 南風原町 + [] → `GET /api/search?searchType=tochi&city={code}&page=N`（地区なし=全域）

## データの持ち方（スナップショット方式）

実行ごとにタイムスタンプ付きファイルを `data/snapshots/` に追加していく（上書きではない）。

```
data/snapshots/
   ├─ 2026-06-20_0800.json
   ├─ 2026-06-21_0800.json
   └─ 2026-06-22_1000.json   ← 実行ごとに増える（ファイル名=YYYY-MM-DD_HHMM）
```

スナップショットの中身（監視単位ごとに物件配列）:
```json
{
  "captured_at": "2026-06-22 10:00:00",
  "watches": {
    "house:okinawashi/noborikawa": {
      "label": "沖縄市・登川",
      "to": [],
      "items": [
        {"id": 5530594, "url": "https://www.e-uchina.net/bukken/house/h-6599-7260616-0389/detail.html", "title": "..."}
      ]
    }
  }
}
```

- 監視単位キー(watch_key): `{search_type}:` を先頭に付け、続けて地区ありは `city_slug/地区slugを+で連結（ソート済）`、全域は `city_slug`。
  例: `house:haebarucho` / `tochi:tomigusukushi` / `house:okinawashi/noborikawa`。
  → 同じ地域でも物件種別が違えば別キーになり、住宅と土地が衝突しない。
- 古いファイルは「本日初回実行時」に **直近20件を残して**削除（件数ベース。比較基準確定後に実行）。
- 比較対象は **「当日 ＋ 当日より前で最も新しいデータ日」の全ファイルのID和集合**（`baseline_reference`）。
  当日2回目以降＝当日分すべて＋直近の過去データ日分すべて／当日初回＝直近の過去データ日分すべて。
  「直近の過去データ日」は何日前でも“その1日”だけを拾う（日数固定の発想ではない）。

## 確定している仕様

- 比較は「当日 ＋ 当日より前で最も新しいデータ日 の全ファイルのID和集合」と行う。
  → 同日内の収集取りこぼし（ページずれ等）でIDが一瞬欠けても、その日の他ファイルが補完するので
    既知物件が「新着」に化けにくい（直近1ファイル比較で起きていた誤検知への対策）。
  → 比較基準に「直近の過去データ日」も常に含めるため、当日初回で取りこぼし→当日2回目で復活した物件も
    その過去データ日に在籍記録があれば再通知されない（当日分だけと比べていた頃の「残る穴」を解消）。
  → 日をまたいだ一時消失→再掲載は、直近の過去データ日の和集合に含まれるため再通知されない
    （当日2回目以降も前データ日を基準に含むので、初回以外でも成立する）。
  → 残る穴: 当日初回の取りこぼしで、かつ直近の過去データ日にも在籍記録が無い物件が当日2回目で
    現れると新着扱いになる（＝真に直近2データ日いずれにも記録が無いケースのみ・稀・許容）。
- **初回実行（過去スナップショットが1つも無い時）はメールを送らず、ベースライン保存のみ**。
- **設定に新しい監視対象を追加した直後**も、その対象は初回はベースライン化し通知しない（追加直後の大量メール防止）。
- **新着が無い実行でも「新着物件なし」通知メールを送る**（システム稼働の確認用）。ただしベースライン作成時（初回・新規追加対象）は送らない。頻繁に実行すると毎回届く点に注意。
- **robots.txt が使用パスを禁止に変えた場合**は収集・通知を中止し、停止理由を知らせる「自動チェックを停止しました」メールを送る（本日初回のみ確認・1日1通・宛先は default_to と各 region の to の和集合）。終了コードは1（異常終了）。`--no-email`/`--dry-run` 時はメールを送らずログのみ。
- 新着メールは物件ごとに空行を入れて表示する。
- 「本日初回」の判定は data/snapshots/ に今日の日付ファイルが無いことで行う。削除は1日1回だけ走る。
- 同じ日に複数回実行してもよい。毎回ファイルが増え、毎回「直前ファイル」と比較する。
- メール送信は **Gmail + アプリパスワード**（SMTP `smtp.gmail.com:587`、STARTTLS）。
  パスワードは **`config/mail_password.txt`（gitignore済）** に保存して読み込む。
  環境変数 `UCHINA_MAIL_PASS` があればそちらを優先（任意のフォールバック）。
  どちらも無ければ送信を自動スキップし、新着はログ出力のみ。
- **バックエンドは Python**。`requests`(API取得) + `PyYAML`(設定) + 標準`smtplib`(メール)。HTMLパース不要のため BeautifulSoup は不使用。

## 使い方 / 運用

> 操作手順（Gmail設定・タスクスケジューラ登録・実行方法）は [docs/document.md](docs/document.md) にまとめてある。

```
# 依存はプロジェクト同梱の仮想環境 .venv に導入済み
python watch.py            # 通常実行（収集→保存→差分→メール）
python watch.py --no-email # メール送信せず差分はログのみ
python watch.py --dry-run  # 保存もメールもしない（収集と差分の確認だけ）

# タスクスケジューラからは run.bat を実行（.venv のPythonを使うのでPATH非依存）
run.bat
```

- ログは `logs/run.log`（UTF-8）。コンソール出力もUTF-8（run.batで `chcp 65001`）。
- Gmail送信を有効化するには、`config/mail_password.txt` にアプリパスワード16文字を保存（または環境変数 `UCHINA_MAIL_PASS` を設定）。
- 定期実行: タスクスケジューラで `run.bat` を1日数回起動。「ログオンしていなくても実行」にする場合も
  run.bat が `.venv` のPythonをフルパスで呼ぶため動作する。

## 開発環境

- **Python 3.13.14 をローカルPCに導入済み**（2026-06-22, Windows 11 Home / AMD64）。
  - winget ユーザースコープ導入、`PrependPath=1` でユーザーPATHへ自動追加済み。
  - 設置先: `C:\Users\nago yuta\AppData\Local\Programs\Python\Python313\`
  - 導入手順の詳細は [docs/python-setup.md](docs/python-setup.md)。
- プロジェクト依存は `.venv`（`requests`, `PyYAML`）。`requirements.txt` 参照。

## 未確定 / 残タスク

- **Gmailアプリパスワードの発行**（送信用アカウントで2段階認証ON → アプリパスワード生成）→ `config/mail_password.txt` に保存。
- タスクスケジューラへの `run.bat` 登録（実行間隔の決定）。
- `config/watchlist.yaml` に実際に監視したい地域・地区・宛先を記入。

## ディレクトリ構成（実装済み）

```
jutaku-research/
├─ CLAUDE.md
├─ requirements.txt        ← Python依存（requests, PyYAML）
├─ watch.py                ← 本体
├─ run.bat                 ← タスクスケジューラ起動用（.venvのPythonを呼ぶ）
├─ .gitignore
├─ docs/
│   ├─ document.md         ← 操作マニュアル（Gmail設定・タスクスケジューラ登録・使い方）
│   └─ python-setup.md     ← Python導入・PATH設定の作業記録
├─ config/
│   └─ watchlist.yaml      ← 監視条件（編集するのはここ）
├─ data/
│   └─ snapshots/          ← 実行ごとのスナップショット（自動生成・直近20件を残し自動削除）
├─ logs/
│   └─ run.log             ← 実行ログ（UTF-8）
└─ .venv/                  ← 仮想環境（gitignore対象）
```
