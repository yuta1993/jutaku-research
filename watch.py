#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
うちなーらいふ 新着物件 通知バッチ
====================================
指定した市町村・地区の物件IDを集めてスナップショットに保存し、
直近スナップショットとの差分（=新規ID）の物件リンクをメールで通知する。

使い方:
    python watch.py                 # 通常実行（収集→保存→差分→メール）
    python watch.py --no-email      # メール送信せず差分はログ出力のみ
    python watch.py --dry-run       # スナップショット保存もメールもしない（収集と差分の確認だけ）
    python watch.py --config path   # 設定ファイルを指定（既定: config/watchlist.yaml）

設計の詳細は CLAUDE.md を参照。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import smtplib
import sys
import time
from email.mime.text import MIMEText
from email.utils import formatdate
from pathlib import Path
from urllib.robotparser import RobotFileParser

import requests
import yaml

# ---- パス定義（このファイルからの相対）-------------------------------------
BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = BASE_DIR / "config" / "watchlist.yaml"
SNAP_DIR = BASE_DIR / "data" / "snapshots"
LOG_DIR = BASE_DIR / "logs"

API_BASE = "https://www.e-uchina.net/api"
SITE_BASE = "https://www.e-uchina.net"
RETENTION_COUNT = 20        # 直近この件数のスナップショットを残し、それ以前を掃除する
SNAP_TS_FMT = "%Y-%m-%d_%H%M"
ROBOTS_URL = SITE_BASE + "/robots.txt"
# 当方が実際にアクセスするパス（すべて /api/ 配下）。本日初回実行時に robots.txt で禁止されていないか確認する。
ROBOTS_CHECK_PATHS = ["/api/search", "/api/area/get_searchable_cities", "/api/area/get_searchable_areas"]

log = logging.getLogger("uchina-watch")

# メールで物件タイトルの下に出せる項目  key: (ラベル, [APIフィールド候補(先頭から最初に値があるものを使う)])
ITEM_DETAIL_CATALOG = {
    "price":         ("価格",     ["price_disp", "price_biko_disp"]),
    "madori":        ("間取り",   ["madori_space_all_disp"]),
    "land":          ("土地面積", ["tochi_metr_disp"]),
    "building":      ("建物面積", ["building_metr_disp"]),
    "built":         ("築年",     ["kenchiku_disp"]),
    "address":       ("住所",     ["address_disp"]),
    "parking":       ("駐車場",   ["short_parking_disp", "parking_disp"]),
    "transport":     ("交通",     ["transport_info"]),
    "madori_detail": ("間取り詳細", ["madori_detail_no_biko_disp"]),
}
DEFAULT_DETAIL_FIELDS = ["price", "madori", "land", "building", "built", "address"]


# ---- ロギング設定 -----------------------------------------------------------
def setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    # Windowsの標準出力は既定でcp932になり日本語が化けるためUTF-8へ統一
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")
    log.setLevel(logging.INFO)
    fh = logging.FileHandler(LOG_DIR / "run.log", encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.addHandler(fh)
    log.addHandler(sh)


# ---- 設定読み込み -----------------------------------------------------------
def _normalize_watches(cfg: dict, path: Path) -> list[dict]:
    """設定を [{search_type, regions:[...]}] の形に正規化する。

    新形式（推奨）: トップレベル `watches: [{search_type, regions:[...]}]`
        物件種別ごとに監視する地域を別々に指定できる。
    旧形式（後方互換）: トップレベル `search_type`(+ `regions`)。
        search_type が文字列でもリストでも受け、各種別に同じ regions を割り当てる。
    """
    raw = cfg.get("watches")
    if raw:
        if not isinstance(raw, list):
            raise ValueError(f"watches はリストで指定してください: {path}")
        blocks = []
        for i, blk in enumerate(raw):
            if not isinstance(blk, dict) or not blk.get("search_type"):
                raise ValueError(f"watches[{i}] には search_type が必要です: {path}")
            blocks.append({
                "search_type": blk["search_type"],
                "regions": blk.get("regions") or [],
            })
        return blocks
    # --- 旧形式フォールバック ---
    if "regions" in cfg:
        st_raw = cfg.get("search_type") or "house"
        search_types = st_raw if isinstance(st_raw, list) else [st_raw]
        return [{"search_type": st, "regions": cfg["regions"]} for st in search_types]
    raise ValueError(f"設定ファイルに watches（または regions）がありません: {path}")


def load_config(path: Path) -> dict:
    with path.open(encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    if not cfg:
        raise ValueError(f"設定ファイルが空です: {path}")
    cfg["watches"] = _normalize_watches(cfg, path)
    cfg.setdefault("http", {})
    cfg["http"].setdefault("user_agent", "uchina-watch/1.0")
    cfg["http"].setdefault("request_interval_sec", 1.5)
    cfg["http"].setdefault("timeout_sec", 30)
    cfg["http"].setdefault("max_pages", 60)
    cfg.setdefault("mail", {})
    cfg["mail"].setdefault("detail_fields", DEFAULT_DETAIL_FIELDS)
    return cfg


# ---- API クライアント -------------------------------------------------------
class UchinaClient:
    def __init__(self, http_cfg: dict, detail_keys: list | None = None):
        self.detail_keys = detail_keys or []
        self.interval = float(http_cfg["request_interval_sec"])
        self.timeout = float(http_cfg["timeout_sec"])
        self.max_pages = int(http_cfg["max_pages"])
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": http_cfg["user_agent"],
            "Accept": "application/json",
            "X-Requested-With": "XMLHttpRequest",
        })
        self._city_map: dict | None = None          # slug -> city_code
        self._area_cache: dict[str, dict] = {}        # city_code -> {slug: area_code}

    def _get(self, url: str, params: dict | None = None, referer: str | None = None) -> dict:
        headers = {"Referer": referer} if referer else {}
        for attempt in (1, 2, 3):
            try:
                r = self.session.get(url, params=params, headers=headers, timeout=self.timeout)
                r.raise_for_status()
                return r.json()
            except Exception as e:
                if attempt == 3:
                    raise
                wait = 2 * attempt
                log.warning("取得失敗(%d回目) %s : %s -> %d秒後に再試行", attempt, url, e, wait)
                time.sleep(wait)
        return {}

    def city_code(self, slug: str) -> str | None:
        if self._city_map is None:
            data = self._get(f"{API_BASE}/area/get_searchable_cities", params={"filter": ""})
            self._city_map = {c["slug"]: c["city_code"] for c in data.get("data", [])}
            time.sleep(self.interval)
        return self._city_map.get(slug)

    def area_codes(self, city_code: str, area_slugs: list[str]) -> list[str]:
        if not area_slugs:
            return []
        if city_code not in self._area_cache:
            data = self._get(f"{API_BASE}/area/get_searchable_areas",
                             params={"city_code": city_code})
            self._area_cache[city_code] = {a["slug"]: a["area_code"] for a in data.get("data", [])}
            time.sleep(self.interval)
        amap = self._area_cache[city_code]
        codes = []
        for s in area_slugs:
            code = amap.get(s)
            if code:
                codes.append(code)
            else:
                log.warning("地区slugが見つかりません(city_code=%s): %s", city_code, s)
        return codes

    def fetch_listings(self, search_type: str, city_code: str, area_codes: list[str], referer: str) -> list[dict]:
        """対象の全ページを巡回し、物件 [{id, url, title}] を返す。"""
        items: dict[int, dict] = {}
        page = 1
        while page <= self.max_pages:
            params = {"searchType": search_type, "city": city_code, "page": page}
            if area_codes:
                params["areas"] = ",".join(area_codes)
            data = self._get(f"{API_BASE}/search", params=params, referer=referer)
            bukkens = (data.get("data") or {}).get("bukkens") or {}
            rows = bukkens.get("data") or []
            for b in rows:
                bid = b.get("id")
                if bid is None:
                    continue
                details = []
                for key in self.detail_keys:
                    entry = ITEM_DETAIL_CATALOG.get(key)
                    if not entry:
                        continue
                    label, fields = entry
                    val = ""
                    for fld in fields:
                        raw = b.get(fld)
                        if raw is None:
                            continue
                        s = str(raw).replace("<br/>", " / ").replace("<br>", " / ").strip()
                        if s:
                            val = s
                            break
                    if val:
                        details.append([label, val])
                items[bid] = {
                    "id": bid,
                    "url": b.get("permalink") or "",
                    "title": (b.get("catch_phrase_web") or "").strip(),
                    "details": details,
                }
            last_page = int(bukkens.get("last_page") or 1)
            log.info("    page %d/%d (%d件)", page, last_page, len(rows))
            if page >= last_page:
                break
            page += 1
            time.sleep(self.interval)
        return list(items.values())


# ---- スナップショット --------------------------------------------------------
def watch_key(region: dict, search_type: str) -> str:
    areas = region.get("areas") or []
    if areas:
        base = region["city_slug"] + "/" + "+".join(sorted(areas))
    else:
        base = region["city_slug"]
    return f"{search_type}:{base}"


def list_snapshots() -> list[Path]:
    if not SNAP_DIR.exists():
        return []
    return sorted(SNAP_DIR.glob("*.json"))


def baseline_reference(now: dt.datetime) -> tuple[dict[str, set], set, str | None]:
    """比較基準を返す。「当日 ＋ 当日より前で最も新しいデータ日」の全スナップショットを束ねる。

    - 当日2回目以降 → 当日全部 ＋ 直近の過去データ日全部 の和集合
    - 当日初回       → 直近の過去データ日全部（カレンダー上何日前でも“その1日”）の和集合
    比較基準に前日（直近の過去データ日）分も含めることで、当日の収集取りこぼしで一時的に
    IDが欠けても既知物件が「新着」に化けない（当日分だけと比べていた頃の誤検知対策）。
    戻り値: (ids_by_key: {watch_key: set(id)}, keys_present: set(watch_key), ref_label: str | None)
            ref_label … 比較に使った日付をカンマ区切りで並べたログ表示用文字列。
            None のとき = スナップショットが1つも無い（＝初回実行）。
    """
    snaps = list_snapshots()
    if not snaps:
        return {}, set(), None
    today = now.strftime("%Y-%m-%d")
    dates = sorted({p.stem.split("_")[0] for p in snaps}, reverse=True)  # 日付・新しい順
    prev_date = next((d for d in dates if d < today), None)  # 当日より前で最新のデータ日
    ref_dates: set[str] = set()
    if dates[0] == today:            # 最新が当日 = 当日ファイルあり（2回目以降）
        ref_dates.add(today)
    if prev_date:                    # 直近の過去データ日（何日前でも“その1日”）
        ref_dates.add(prev_date)
    # 当日初回（当日ファイル無し）→ ref_dates は {prev_date} のみ＝従来どおり
    ids_by_key: dict[str, set] = {}
    keys_present: set = set()
    for p in snaps:
        if p.stem.split("_")[0] not in ref_dates:
            continue
        try:
            with p.open(encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            log.warning("スナップショット読み込み失敗 %s : %s", p, e)
            continue
        for key, info in (data.get("watches") or {}).items():
            keys_present.add(key)
            s = ids_by_key.setdefault(key, set())
            for it in info.get("items", []):
                if it.get("id") is not None:
                    s.add(it["id"])
    return ids_by_key, keys_present, ", ".join(sorted(ref_dates))


def save_snapshot(now: dt.datetime, watches: dict) -> Path:
    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    path = SNAP_DIR / (now.strftime(SNAP_TS_FMT) + ".json")
    payload = {"captured_at": now.strftime("%Y-%m-%d %H:%M:%S"), "watches": watches}
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    return path


def cleanup_old_snapshots() -> None:
    """本日初回実行時のみ呼ぶ。直近 RETENTION_COUNT 件を残し、それ以前を削除する。

    ※ 比較基準（baseline_reference）を確定した後に呼ぶこと（比較対象を消さないため）。
    """
    snaps = list_snapshots()                         # 名前順＝時系列順
    for p in snaps[:-RETENTION_COUNT]:               # 直近 RETENTION_COUNT 件より古いもの
        p.unlink(missing_ok=True)
        log.info("古いスナップショットを削除: %s", p.name)


def is_first_run_today(now: dt.datetime) -> bool:
    today_prefix = now.strftime("%Y-%m-%d")
    return not any(p.name.startswith(today_prefix) for p in list_snapshots())


# ---- robots.txt 確認 --------------------------------------------------------
def check_robots(http_cfg: dict) -> bool | None:
    """robots.txt を取得し、当方が使う /api/ 配下へのアクセス可否を確認する。

    戻り値:
        True  … 許可されている（または該当ルールが無く問題なし）
        False … 明確に禁止されている → 収集・通知を中止すべき
        None  … robots.txt を取得・解析できなかった（判断不能）→ 通常実行を続行
    """
    ua = http_cfg.get("user_agent") or "uchina-watch/1.0"
    try:
        r = requests.get(ROBOTS_URL, headers={"User-Agent": ua},
                         timeout=float(http_cfg.get("timeout_sec", 30)))
        r.raise_for_status()
    except Exception as e:
        log.warning("robots.txt を取得できませんでした（%s）。判断不能のため通常実行を続行します。", e)
        return None
    try:
        rp = RobotFileParser()
        rp.parse(r.text.splitlines())
    except Exception as e:
        log.warning("robots.txt の解析に失敗しました（%s）。判断不能のため通常実行を続行します。", e)
        return None
    blocked = [p for p in ROBOTS_CHECK_PATHS if not rp.can_fetch(ua, SITE_BASE + p)]
    if blocked:
        log.error("robots.txt が次のパスへのアクセスを禁止しています: %s", ", ".join(blocked))
        return False
    log.info("robots.txt 確認OK: 使用パス（%s）へのアクセスは許可されています。", ", ".join(ROBOTS_CHECK_PATHS))
    return True


def _robots_alert_marker() -> Path:
    return SNAP_DIR / ".robots_alert_last"


def robots_alert_sent_today(now: dt.datetime) -> bool:
    """本日すでに robots停止アラートを送信済みなら True（1日1通に絞るため）。"""
    try:
        return _robots_alert_marker().read_text(encoding="utf-8").strip() == now.strftime("%Y-%m-%d")
    except Exception:
        return False


def record_robots_alert(now: dt.datetime) -> None:
    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    _robots_alert_marker().write_text(now.strftime("%Y-%m-%d"), encoding="utf-8")


# ---- メール -----------------------------------------------------------------
def build_email_body(new_by_watch: dict) -> str:
    lines = ["うちなーらいふに新着物件が出ました。\n"]
    for key, info in new_by_watch.items():
        lines.append(f"■ {info['label']}（{key}） {len(info['items'])}件")
        for it in info["items"]:
            title = (it["title"] or "(物件)").replace("\n", " ").strip()
            if len(title) > 70:
                title = title[:70] + "…"
            lines.append(f"  - {title}")
            for label, val in it.get("details", []):
                lines.append(f"      {label}: {val}")
            lines.append(f"    {it['url']}")
            lines.append("")          # 物件ごとに空行を入れて読みやすく
        lines.append("")
    return "\n".join(lines)


def group_recipients(new_by_watch: dict, default_to: list[str]) -> dict:
    """宛先 -> その宛先に送るべき new_by_watch のサブセット。"""
    by_to: dict[str, dict] = {}
    for key, info in new_by_watch.items():
        tos = info["to"] or default_to
        for addr in tos:
            by_to.setdefault(addr, {})[key] = info
    return by_to


def resolve_mail_password(mail_cfg: dict) -> str:
    """メール送信パスワードを取得する。環境変数(設定されていれば優先) → password_file の順。"""
    # 1) 環境変数（あればこちらを優先）
    env_name = mail_cfg.get("password_env")
    if env_name:
        val = os.environ.get(env_name, "")
        if val.strip():
            return "".join(val.split())
    # 2) ファイル（中身をパスワードとして読む。空白・改行・#コメント行は無視）
    pf = mail_cfg.get("password_file")
    if pf:
        path = Path(pf)
        if not path.is_absolute():
            path = BASE_DIR / path
        try:
            content = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            log.warning("password_file が見つかりません: %s", path)
            return ""
        except Exception as e:
            log.warning("password_file の読み込み失敗 %s : %s", path, e)
            return ""
        for line in content.splitlines():
            s = line.strip()
            if s and not s.startswith("#"):
                return "".join(s.split())   # アプリパスワードに空白は無いので除去
        log.warning("password_file が空です: %s", path)
    return ""


def _smtp_send(mail_cfg: dict, password: str, messages: list) -> None:
    """messages: [(to_addr, subject, body), ...] をまとめて送信する。"""
    from_addr = mail_cfg.get("from_addr") or mail_cfg["user"]
    with smtplib.SMTP(mail_cfg["smtp_host"], int(mail_cfg["smtp_port"]), timeout=30) as smtp:
        smtp.starttls()
        smtp.login(mail_cfg["user"], password)
        for addr, subject, body in messages:
            msg = MIMEText(body, "plain", "utf-8")
            msg["Subject"] = subject
            msg["From"] = from_addr
            msg["To"] = addr
            msg["Date"] = formatdate(localtime=True)
            smtp.sendmail(from_addr, [addr], msg.as_string())


def send_emails(mail_cfg: dict, new_by_watch: dict) -> None:
    password = resolve_mail_password(mail_cfg)
    default_to = mail_cfg.get("default_to") or []
    if not password:
        log.warning("メール送信パスワードを取得できません（password_file または 環境変数 %s を確認）。"
                    " 送信をスキップします（新着は上記ログ参照）。", mail_cfg.get("password_env"))
        return

    by_to = group_recipients(new_by_watch, default_to)
    if not by_to:
        log.warning("宛先が設定されていないためメールを送信できません。")
        return

    total_new = sum(len(i["items"]) for i in new_by_watch.values())
    subject = f"【うちなーらいふ】新着物件 {total_new}件"
    _smtp_send(mail_cfg, password, [(addr, subject, build_email_body(subset)) for addr, subset in by_to.items()])
    for addr, subset in by_to.items():
        log.info("メール送信: %s (%d件)", addr, sum(len(i["items"]) for i in subset.values()))


def build_no_new_body(watches: dict) -> str:
    lines = ["うちなーらいふの新着チェックを行いましたが、新着物件はありませんでした。", ""]
    lines.append("【確認した監視対象（現在の掲載件数）】")
    prev_type = None
    for key, info in watches.items():
        cur_type = key.split(":", 1)[0]          # watch_key 先頭が物件種別（house/tochi/mansion）
        if prev_type is None:
            pass                                  # 最初の項目は区切りなし
        elif cur_type != prev_type:
            lines.extend(["", ""])                # 種別の境目は空行2つ（2行改行）
        else:
            lines.append("")                      # 同じ種別内のエリアの境目は空行1つ（1行改行）
        lines.append(f"  - {info['label']}（{key}） {len(info['items'])}件")
        prev_type = cur_type
    lines.append("")
    return "\n".join(lines)


def send_no_new_email(mail_cfg: dict, watches: dict) -> None:
    """新着が無かった場合に「新着なし」を通知する。"""
    password = resolve_mail_password(mail_cfg)
    default_to = mail_cfg.get("default_to") or []
    if not password:
        log.warning("メール送信パスワードを取得できません（password_file または 環境変数 %s を確認）。"
                    " 送信をスキップします。", mail_cfg.get("password_env"))
        return
    recipients = sorted({a for info in watches.values() for a in (info.get("to") or default_to)})
    if not recipients:
        log.warning("宛先が設定されていないためメールを送信できません。")
        return
    subject = "【うちなーらいふ】新着物件なし"
    body = build_no_new_body(watches)
    _smtp_send(mail_cfg, password, [(addr, subject, body) for addr in recipients])
    for addr in recipients:
        log.info("メール送信(新着なし): %s", addr)


def all_recipients(cfg: dict) -> list[str]:
    """通知を受け取りうる全宛先（default_to + 各regionのto）の和集合。

    robots停止アラートは特定の監視対象に紐づかないシステム通知のため、
    普段いずれかの通知を受け取る人すべてに知らせる。
    """
    addrs = set(cfg.get("mail", {}).get("default_to") or [])
    for block in cfg.get("watches", []):
        for region in block.get("regions") or []:
            for a in (region.get("to") or []):
                addrs.add(a)
    return sorted(addrs)


def build_robots_alert_body() -> str:
    return "\n".join([
        "うちなーらいふの robots.txt が変更され、データ取得に使用している",
        "/api/ へのアクセスが現在は許可されていません。",
        "サイトの方針変更の可能性があるため、自動チェックを停止しました。",
        "",
        "新着通知はこの問題が解決するまで届きません。",
        "詳細は logs/run.log をご確認ください。",
        "",
        "-- ",
    ])


def send_robots_alert(mail_cfg: dict, recipients: list[str]) -> None:
    """robots.txt によりアクセスが禁止された旨を通知する（中止時のみ）。"""
    password = resolve_mail_password(mail_cfg)
    if not password:
        log.warning("メール送信パスワードを取得できないため、robots停止アラートを送信できません。")
        return
    if not recipients:
        log.warning("宛先が設定されていないため、robots停止アラートを送信できません。")
        return
    subject = "【うちなーらいふ】自動チェックを停止しました（robots.txt変更）"
    body = build_robots_alert_body()
    _smtp_send(mail_cfg, password, [(addr, subject, body) for addr in recipients])
    for addr in recipients:
        log.info("メール送信(robots停止アラート): %s", addr)


# ---- メイン -----------------------------------------------------------------
def run(cfg: dict, no_email: bool, dry_run: bool) -> int:
    now = dt.datetime.now()
    first_today = is_first_run_today(now)

    # 比較基準（当日＋直近の過去データ日の全ファイルのID和集合）を、掃除より前に確定する
    base_ids, base_keys, ref_date = baseline_reference(now)

    # ① 本日初回なら robots.txt 確認 → 古いスナップショット掃除（比較基準を確定した後で）
    if first_today:
        # robots.txt 確認（本日初回のみ）。禁止なら中止＋アラートメール（1日1通）。
        if check_robots(cfg["http"]) is False:
            log.error("robots.txt によりアクセスが許可されていないため、収集・通知を中止します。")
            if dry_run:
                log.info("dry-run: robots停止アラートメールは送信しません。")
            elif no_email:
                log.info("--no-email 指定のため robots停止アラートメールは送信しません。")
            elif robots_alert_sent_today(now):
                log.info("本日はすでに robots停止アラートを送信済みのため、メールは送りません。")
            else:
                send_robots_alert(cfg["mail"], all_recipients(cfg))
                record_robots_alert(now)
            return 1
        if not dry_run:
            cleanup_old_snapshots()

    client = UchinaClient(cfg["http"], cfg["mail"].get("detail_fields"))

    # ② 全監視対象（物件種別×地域）を巡回してID収集
    watches: dict[str, dict] = {}
    for block in cfg["watches"]:
        search_type = block["search_type"]
        for region in block["regions"]:
            key = watch_key(region, search_type)
            label = region.get("label", key)
            log.info("収集: %s [%s]", label, key)
            city_code = client.city_code(region["city_slug"])
            if not city_code:
                log.error("  市町村slugが見つかりません: %s （スキップ）", region["city_slug"])
                continue
            area_codes = client.area_codes(city_code, region.get("areas") or [])
            human_parts = [search_type, region["city_slug"]]
            if region.get("areas"):
                human_parts.append(region["areas"][0])
            referer = f"{SITE_BASE}/" + "/".join(human_parts)
            items = client.fetch_listings(search_type, city_code, area_codes, referer)
            watches[key] = {
                "label": label,
                "to": region.get("to") or [],
                "items": items,
            }
            log.info("  -> %d件", len(items))
            time.sleep(client.interval)

    # ③ 差分判定（当日＋直近の過去データ日の全ファイルのID和集合と比較）
    new_by_watch: dict[str, dict] = {}
    baseline = ref_date is None
    if baseline:
        log.info("過去スナップショットが無いため、今回はベースライン作成のみ（メール送信なし）。")
    else:
        log.info("比較基準: %s のスナップショット（%d監視対象）の和集合と比較。", ref_date, len(base_keys))
        for key, info in watches.items():
            if key not in base_keys:
                log.info("新しい監視対象 [%s] は今回ベースライン化（通知なし）。", key)
                continue
            prev_ids = base_ids.get(key, set())
            new_items = [it for it in info["items"] if it["id"] not in prev_ids]
            if new_items:
                new_by_watch[key] = {"label": info["label"], "to": info["to"], "items": new_items}
                log.info("新着 [%s]: %d件", key, len(new_items))

    # 新着サマリをログ
    if not baseline:
        total_new = sum(len(i["items"]) for i in new_by_watch.values())
        log.info("新着合計: %d件", total_new)
        for key, info in new_by_watch.items():
            for it in info["items"]:
                log.info("  NEW [%s] %s %s", key, it["title"] or "(物件)", it["url"])

    # ④ スナップショット保存
    if dry_run:
        log.info("dry-run: スナップショット保存・メール送信は行いません。")
        return 0
    path = save_snapshot(now, watches)
    log.info("スナップショット保存: %s", path.name)

    # ⑤ メール送信
    if no_email:
        log.info("--no-email 指定のためメール送信はスキップ。")
    elif baseline:
        pass  # ベースライン作成時はメールを送らない（新着なし通知も出さない）
    elif new_by_watch:
        send_emails(cfg["mail"], new_by_watch)
    else:
        log.info("新着なし → 「新着物件なし」通知メールを送信します。")
        send_no_new_email(cfg["mail"], watches)

    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="うちなーらいふ 新着物件 通知バッチ")
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--no-email", action="store_true", help="メール送信せず差分はログのみ")
    ap.add_argument("--dry-run", action="store_true", help="保存もメールもしない（確認用）")
    args = ap.parse_args()

    setup_logging()
    log.info("=== uchina-watch 開始 ===")
    try:
        cfg = load_config(args.config)
        rc = run(cfg, no_email=args.no_email, dry_run=args.dry_run)
    except Exception as e:
        log.exception("実行中にエラー: %s", e)
        return 1
    log.info("=== uchina-watch 終了 ===")
    return rc


if __name__ == "__main__":
    sys.exit(main())
