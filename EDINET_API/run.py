#!/usr/bin/env python3
"""
EDINET 財務諸表ダウンローダー
==============================
製造業全社の10年分財務諸表を data/ フォルダにダウンロードします。

使い方:
    cd EDINET_API
    python run.py

事前設定:
    1. config.py の EDINET_API_KEY を入力
       または環境変数 EDINET_API_KEY を設定
    2. 必要ライブラリをインストール:
       pip install requests python-dateutil

取得内容:
    - 有価証券報告書 / 訂正有価証券報告書 / 半期報告書 / 四半期報告書
    - フォーマット: XBRL ZIP (財務数値), PDF (NLP向け), CSV ZIP (簡易解析)
    - 対象: 製造業16業種 (食料品〜その他製品)
    - 期間: 過去10年分

再実行:
    中断後に再実行すると、済み月・済みファイルは自動スキップします。
    progress.json を削除するとリセットされます。

ディレクトリ構造 (出力):
    data/
    └── {edinetCode}_{企業名}/
        └── {docTypeCode}_{periodEnd}/
            ├── xbrl_{docID}.zip  ← XBRL
            ├── pdf_{docID}.pdf   ← PDF
            └── csv_{docID}.zip   ← CSV
"""

import sys
from pathlib import Path

# EDINET_API フォルダをパスに追加（どこから実行しても動くように）
sys.path.insert(0, str(Path(__file__).resolve().parent))

from downloader import run

if __name__ == "__main__":
    run()