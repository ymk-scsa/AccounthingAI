"""
EDINET ダウンローダー
======================
製造業全社の財務諸表（有価証券報告書等）を10年分ダウンロードする。

取得フォーマット（config.py で ON/OFF 可能）:
  - XBRL ZIP: 財務数値の機械解析向け
  - PDF:      NLP / テキスト分析向け
  - CSV ZIP:  財務数値の簡易解析向け (EDINET v2 から追加)

ディレクトリ構造:
  data/
  └── {edinetCode}_{企業名}/
      └── {docTypeCode}_{periodEnd}/
          ├── xbrl_{docID}.zip
          ├── pdf_{docID}.pdf
          └── csv_{docID}.zip
"""

import io
import json
import logging
import time
import zipfile
from datetime import datetime
from pathlib import Path

import requests
from dateutil.relativedelta import relativedelta

from config import (
    DATA_DIR,
    DOWNLOAD_INTERVAL_SEC,
    DOWNLOAD_TYPES,
    EDINET_BASE_URL,
    EDINET_CODE_LIST_URL,
    EDINET_DIR,
    EDINET_API_KEY,
    MANUFACTURING_CODES,
    MAX_RETRY,
    REQUEST_INTERVAL_SEC,
    RETRY_WAIT_SEC,
    SCAN_YEARS,
    TARGET_DOC_TYPES,
)

# ============================================================
# ログ設定
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(EDINET_DIR / "download.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)


# ============================================================
# 進捗管理（再実行時に済みの日付をスキップ）
# ============================================================
PROGRESS_FILE = EDINET_DIR / "progress.json"


def load_progress() -> dict:
    if PROGRESS_FILE.exists():
        with open(PROGRESS_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"scanned_days": [], "downloaded": []}


def save_progress(progress: dict) -> None:
    with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
        json.dump(progress, f, ensure_ascii=False, indent=2)


# ============================================================
# HTTP ユーティリティ
# ============================================================
def _get(url: str, params: dict, stream: bool = False) -> requests.Response | None:
    """リトライ付き GET リクエスト"""
    for attempt in range(1, MAX_RETRY + 1):
        try:
            res = requests.get(url, params=params, stream=stream, timeout=30)
            if res.status_code == 200:
                return res
            if res.status_code == 404:
                log.warning("404 (閲覧期間外の可能性): %s", url)
                return None
            log.warning("HTTP %s (試行 %d/%d): %s", res.status_code, attempt, MAX_RETRY, url)
        except requests.RequestException as e:
            log.warning("接続エラー (試行 %d/%d): %s", attempt, MAX_RETRY, e)
        if attempt < MAX_RETRY:
            time.sleep(RETRY_WAIT_SEC)
    log.error("失敗 (リトライ上限): %s", url)
    return None


# ============================================================
# Step 1: EDINETコードリストの取得（製造業フィルタ）
# ============================================================
def fetch_edinet_company_list() -> dict[str, dict]:
    """
    EDINETコードリストCSVをダウンロードし、製造業企業を返す。

    戻り値:
        {edinetCode: {"name": 提出者名, "industry": 提出者業種}} の dict
    """
    log.info("EDINETコードリストを取得中...")
    cache_path = EDINET_DIR / "EdinetcodeDlInfo.csv"

    # キャッシュが当日のものならそちらを使う
    if cache_path.exists():
        age_days = (datetime.now() - datetime.fromtimestamp(cache_path.stat().st_mtime)).days
        if age_days < 1:
            log.info("キャッシュを使用: %s", cache_path)
            return _parse_edinet_code_csv(cache_path)

    res = _get(EDINET_CODE_LIST_URL, params={})
    if res is None:
        if cache_path.exists():
            log.warning("取得失敗 → 既存キャッシュを使用")
            return _parse_edinet_code_csv(cache_path)
        raise RuntimeError("EDINETコードリストの取得に失敗しました。ネットワークを確認してください。")

    # ZIP を展開して CSV を保存
    with zipfile.ZipFile(io.BytesIO(res.content)) as z:
        csv_name = next(n for n in z.namelist() if n.endswith(".csv"))
        with z.open(csv_name) as f:
            cache_path.write_bytes(f.read())

    log.info("EDINETコードリスト保存: %s", cache_path)
    return _parse_edinet_code_csv(cache_path)


def _parse_edinet_code_csv(csv_path: Path) -> dict[str, dict]:
    """
    EdinetcodeDlInfo.csv をパースして製造業企業マップを返す。

    CSV フォーマット（Shift-JIS, 1行目=メタ情報, 2行目=ヘッダー）:
      ＥＤＩＮＥＴコード, 提出者種別, 上場区分, 連結の有無,
      資本金, 決算日, 提出者名, 提出者名（英字）, 提出者名（ヨミ）,
      所在地, 提出者業種, 証券コード, 提出者法人番号
    """
    import csv

    companies: dict[str, dict] = {}
    with open(csv_path, encoding="cp932", newline="") as f:
        # 1行目はメタ情報（列数が違う）→ スキップ
        f.readline()
        reader = csv.DictReader(f)
        for row in reader:
            edinet_code = row.get("ＥＤＩＮＥＴコード", "").strip()
            industry    = row.get("提出者業種", "").strip()
            name        = row.get("提出者名", "").strip()

            if not edinet_code:
                continue

            # 業種コードを抽出（"食料品（3050）" → "3050"）
            code = _extract_industry_code(industry)
            if code in MANUFACTURING_CODES:
                companies[edinet_code] = {"name": name, "industry": industry, "code": code}

    log.info("製造業企業数: %d 社", len(companies))
    return companies


def _extract_industry_code(industry_str: str) -> str:
    """
    「食料品（3050）」や「3050」などから数値コードを抽出する。
    EDINETコードリストの業種表記が揺れるため複数パターンに対応。
    """
    import re
    # 括弧内の数字（例: 食料品（3050））
    m = re.search(r"[（(](\d{4})[）)]", industry_str)
    if m:
        return m.group(1)
    # 数字のみ（例: 3050）
    m = re.search(r"\b(\d{4})\b", industry_str)
    if m:
        return m.group(1)
    # 業種名そのものが一致する場合（例: 食料品）
    from config import MANUFACTURING_INDUSTRIES
    for name, code in MANUFACTURING_INDUSTRIES.items():
        if name in industry_str:
            return code
    return ""


# ============================================================
# Step 2: 書類一覧の取得
# ============================================================
def fetch_document_list(date: datetime) -> list[dict]:
    """指定日付の提出書類一覧を返す（type=2: 書類一覧＋メタデータ）"""
    url = f"{EDINET_BASE_URL}/documents.json"
    params = {
        "date": date.strftime("%Y-%m-%d"),
        "type": 2,
        "Subscription-Key": EDINET_API_KEY,
    }
    res = _get(url, params)
    if res is None:
        return []
    return res.json().get("results", []) or []


def filter_target_docs(docs: list[dict], edinet_codes: set[str]) -> list[dict]:
    """製造業対象企業の、対象書類種別のみ抽出する"""
    return [
        d for d in docs
        if d.get("edinetCode") in edinet_codes
        and str(d.get("docTypeCode", "")) in TARGET_DOC_TYPES
    ]


# ============================================================
# Step 3: ダウンロード
# ============================================================
def _make_save_dir(doc: dict, company_name: str) -> Path:
    """
    data/{edinetCode}_{企業名}/{docTypeCode}_{periodEnd}/
    を作成して返す。
    """
    safe_name = company_name.replace("/", "・").replace("\\", "・")[:40]
    edinet_code = doc["edinetCode"]
    doc_type    = doc.get("docTypeCode", "000")
    period_end  = (doc.get("periodEnd") or doc.get("submitDateTime", "")[:10]).replace("-", "")

    folder = DATA_DIR / f"{edinet_code}_{safe_name}" / f"{doc_type}_{period_end}"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def download_doc(doc: dict, company_name: str, progress: dict) -> None:
    """
    1書類について有効なフォーマット（XBRL/PDF/CSV）を全てダウンロードする。
    各フォーマットのフラグ（xbrlFlag, pdfFlag, csvFlag）を確認してから実行。
    """
    doc_id   = doc["docID"]
    save_dir = _make_save_dir(doc, company_name)

    # フォーマットとフラグのマッピング
    flag_map = {
        "xbrl": doc.get("xbrlFlag") == "1",
        "pdf":  doc.get("pdfFlag")  == "1",
        "csv":  doc.get("csvFlag")  == "1",
    }

    for fmt, cfg in DOWNLOAD_TYPES.items():
        if not cfg["enabled"]:
            continue
        if not flag_map.get(fmt, False):
            continue  # この書類にそのフォーマットが存在しない

        save_path = save_dir / f"{fmt}_{doc_id}{cfg['ext']}"
        progress_key = f"{doc_id}_{fmt}"

        # 重複スキップ（ファイル存在 or 進捗記録）
        if save_path.exists() or progress_key in progress["downloaded"]:
            continue

        url = f"{EDINET_BASE_URL}/documents/{doc_id}"
        params = {"type": cfg["type_id"], "Subscription-Key": EDINET_API_KEY}

        res = _get(url, params, stream=True)
        if res is None:
            continue

        with open(save_path, "wb") as f:
            for chunk in res.iter_content(chunk_size=8192):
                f.write(chunk)

        progress["downloaded"].append(progress_key)
        log.info("✅ %s [%s] %s → %s", company_name, fmt.upper(), doc_id, save_path.name)
        time.sleep(DOWNLOAD_INTERVAL_SEC)


# ============================================================
# メイン処理
# ============================================================
def run() -> None:
    """
    メインダウンロード処理。
    全日スキャン戦略（3,650日）で抜けなく10年分を取得する。
    """
    # API キー確認
    if EDINET_API_KEY == "YOUR_API_KEY_HERE":
        log.error(
            "APIキーが設定されていません。\n"
            "config.py の EDINET_API_KEY を編集するか、\n"
            "環境変数 EDINET_API_KEY を設定してください。"
        )
        return

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    progress = load_progress()

    # Step 1: 製造業企業リスト取得
    companies = fetch_edinet_company_list()
    edinet_codes = set(companies.keys())
    log.info("対象: 製造業 %d 社", len(edinet_codes))

    # Step 2: スキャン対象の全日リストを生成（3,650日）
    from datetime import timedelta
    today      = datetime.today()
    start_date = today - timedelta(days=365 * SCAN_YEARS)

    scan_days: list[datetime] = []
    cur = start_date
    while cur <= today:
        day_key = cur.strftime("%Y-%m-%d")
        if day_key not in progress["scanned_days"]:
            scan_days.append(cur)
        cur += timedelta(days=1)

    total_days = 365 * SCAN_YEARS
    log.info(
        "スキャン対象: %d 日分 (済み %d 日をスキップ)",
        len(scan_days),
        total_days - len(scan_days),
    )

    # Step 3: 1日ずつスキャン → ダウンロード
    total_docs = 0
    for i, day in enumerate(scan_days, 1):
        day_key = day.strftime("%Y-%m-%d")
        log.info("[%d/%d] 📅 %s をスキャン中...", i, len(scan_days), day_key)

        docs = fetch_document_list(day)
        targets = filter_target_docs(docs, edinet_codes)

        if targets:
            log.info("  → 対象書類 %d 件", len(targets))
            for doc in targets:
                code = doc["edinetCode"]
                name = companies[code]["name"]
                download_doc(doc, name, progress)
                total_docs += 1
                time.sleep(REQUEST_INTERVAL_SEC)

        # 日のスキャン完了を記録
        progress["scanned_days"].append(day_key)
        save_progress(progress)

    log.info("=" * 60)
    log.info("完了: 処理書類数 %d 件", total_docs)
    log.info("保存先: %s", DATA_DIR)
    log.info("=" * 60)