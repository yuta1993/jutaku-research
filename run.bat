@echo off
REM うちなーらいふ 新着物件通知 - タスクスケジューラ起動用
REM プロジェクト同梱の仮想環境(.venv)のPythonで watch.py を実行する。
REM PATHに依存しないため、別アカウント/ログオフ中の実行でも動く。
setlocal
chcp 65001 >nul
cd /d "%~dp0"
".venv\Scripts\python.exe" "watch.py" %*
endlocal
