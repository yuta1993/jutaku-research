# Python インストール / PATH 設定の記録

このプロジェクトのバックエンドに使う Python をローカルPCへ導入した際の作業記録。
再現できるよう、実際に実行したコマンド・設置先・確認結果をそのまま残す。

- 作業日: 2026-06-22
- 対象PC: Microsoft Windows 11 Home / アーキテクチャ AMD64
- 作業ユーザー: `nago yuta`

---

## 1. 事前確認（インストール前の状態）

```powershell
# python / py が入っていないことを確認
Get-Command python   # → not found
Get-Command py       # → not found
where.exe python     # → 何も返らない

# インストール手段 winget が使えることを確認
winget --version     # → v1.28.240
```

結果: Python は未導入。winget は利用可能だったため **winget で導入**することにした。

> メモ: ユーザーPATHには既に `C:\xampp\php` が含まれていた（XAMPP由来のPHP）。
> 今回 Python を選定したため未使用だが、環境の事実として記録しておく。

---

## 2. 採用したバージョンと理由

- **Python 3.13.14** を採用。
- 理由: 3.14 系も選べたが、利用予定のライブラリ（`requests` / `beautifulsoup4` / `lxml` / `PyYAML`）の
  対応・配布ホイールが最も枯れている安定版を優先した。

winget で選択可能だったバージョンの確認:
```powershell
winget search --id Python.Python --source winget
# → Python 3.13 (Python.Python.3.13) 3.13.14 などが一覧表示される
```

---

## 3. インストール手順（実行したコマンド）

**管理者権限は不要**。ユーザースコープ（`--scope user`）で導入し、
インストーラ引数 `PrependPath=1` で **PATH への自動追加**まで一度に行った。

```powershell
winget install --id Python.Python.3.13 --source winget --scope user --silent `
  --accept-package-agreements --accept-source-agreements `
  --custom "PrependPath=1 Include_launcher=1"
```

- `--scope user` … 現在のユーザー領域へインストール（管理者昇格なし）
- `--silent` … インストーラUIを出さずに進める
- `--custom "PrependPath=1 Include_launcher=1"` … python.org インストーラへの追加引数。
  - `PrependPath=1` … python本体・Scripts・Launcher のフォルダを**ユーザーPATHへ追加**
  - `Include_launcher=1` … `py` ランチャーを同梱

結果: `インストールが完了しました` と表示され成功。

---

## 4. インストール先（設置パス）

| 内容 | パス |
|---|---|
| Python 本体 | `C:\Users\nago yuta\AppData\Local\Programs\Python\Python313\python.exe` |
| pip | 同梱（`python -m pip` で実行） pip 26.1.2 |
| py ランチャー | `C:\Users\nago yuta\AppData\Local\Programs\Python\Launcher\py.exe` |

---

## 5. PATH はどう通ったか

`PrependPath=1` により、**ユーザー環境変数 Path（レジストリ `HKCU`）** の先頭へ
以下3つのフォルダが自動追加された。手動でのPATH編集は行っていない。

```
C:\Users\nago yuta\AppData\Local\Programs\Python\Python313\Scripts\
C:\Users\nago yuta\AppData\Local\Programs\Python\Python313\
C:\Users\nago yuta\AppData\Local\Programs\Python\Launcher\
```

確認コマンド（ユーザーPATHの中身を表示）:
```powershell
[Environment]::GetEnvironmentVariable('Path','User')
```

---

## 6. 動作確認

インストール直後はフルパス指定で動作確認した（理由は次節）。

```powershell
$base = "$env:LOCALAPPDATA\Programs\Python\Python313"
& "$base\python.exe" --version          # → Python 3.13.14
& "$base\python.exe" -m pip --version    # → pip 26.1.2 ...
& "$env:LOCALAPPDATA\Programs\Python\Launcher\py.exe" --version   # → Python 3.13.14
```

---

## 7. 反映タイミングの注意（重要）

PATH の変更は**レジストリには即時反映**されるが、
**すでに起動しているプロセス（既存のターミナル等）には反映されない**。
これは Windows の仕様で、各プロセスは起動時の環境変数を引き継ぐため。

- ✅ **新しく開いた** PowerShell / コマンドプロンプトでは `python` / `py` がそのまま使える。
- ❌ インストール前から開いていたターミナルでは反映されない（再起動が必要）。

新しいターミナルでの確認:
```powershell
python --version   # → Python 3.13.14
py --version       # → Python 3.13.14
pip --version
```

うまく反映されない場合はPCを一度サインアウト/再起動すれば確実に反映される。

---

## 8. 手動でPATHを通す場合（フォールバック）

今回は `PrependPath=1` で自動追加されたため不要だったが、
別PCで自動追加されなかった場合の手動手順を残す。

### GUI から
1. スタート →「環境変数」で検索 →「環境変数を編集」（ユーザー環境変数でOK）
2. ユーザー環境変数の `Path` を選び「編集」
3. 上記3フォルダ（`...\Python313\`、`...\Python313\Scripts\`、`...\Launcher\`）を「新規」で追加
4. OKで閉じ、**新しいターミナルを開く**

### PowerShell から（ユーザーPATHへ追記）
```powershell
$pyBase = "$env:LOCALAPPDATA\Programs\Python\Python313"
$add = @("$pyBase\", "$pyBase\Scripts\", "$env:LOCALAPPDATA\Programs\Python\Launcher\")
$cur = [Environment]::GetEnvironmentVariable('Path','User')
foreach ($p in $add) { if ($cur -notlike "*$p*") { $cur = "$p;$cur" } }
[Environment]::SetEnvironmentVariable('Path', $cur, 'User')
# 反映には新しいターミナルを開くこと
```

---

## 9. 参考: アンインストール / 再インストール

```powershell
winget uninstall --id Python.Python.3.13
winget upgrade  --id Python.Python.3.13   # 更新
```
