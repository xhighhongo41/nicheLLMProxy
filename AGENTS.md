# AGENTS.md

このファイルは、このリポジトリで作業するAIコーディングエージェント向けの指示書である。

## 文書の役割分担

- `README.md`: ユーザー向け文書の正本(英語)。`README_ja.md` に日本語の等価版を常に維持する。
- `CHANGELOG.md`: ユーザー向け変更履歴(英語)。
- `PROJECT.md`: プロジェクト進行の根幹(git追跡対象外)。ユーザー記載のTODOと各版サマリを置く。
- `開発資料/`: 各版の実装計画・実装記録・調査資料(git追跡対象外)。設計・実装・検証の詳細はすべてここに記載する。

## PROJECT.mdへの記載ルール(長文化防止)

- PROJECT.mdのTODO欄・バージョン履歴欄に長文を書かない。各エントリは1〜2行のサマリと `開発資料/vX.Y実装計画.md`・`開発資料/vX.Y実装記録.md` への参照に留める。
- 設計・実装・リリースの状況報告も、詳細は開発資料内の該当版文書に記載し、PROJECT.mdにはサマリのみを置く。
- 完了済みバージョンのTODO節は、ユーザー記載分を `開発資料/PROJECTアーカイブ.md` へ移動したうえで削除し、バージョン履歴へ統合する。

## 開発サイクル

- バージョン開発は version-start(設計)→ version-implement(実装)→ version-release(リリース)のサイクルで行う。
- 作業ブランチ名は版番号そのもの(例: `1.4`)。タグは `vX.Y.Z` 形式でリリース時に付与する。
- コミットメッセージは英語。コミット単位は論理的な作業単位とする。
- コミット前に `tools/check.sh` が全通過することを確認する。

## 言語運用

- コミットメッセージ: 英語。
- README.md・CHANGELOG.md: 英語(README_ja.mdが日本語等価版)。
- PROJECT.md・開発資料・本ファイル: 日本語。
- コード内のメッセージ(i18n): msgidは英語。jaカタログに翻訳を追記し、`msgfmt --check --output-file` で .mo を再生成する。詳細は `src/niche_llm_proxy/i18n.py` のdocstring参照。

## 検証

- `tools/check.sh`: uv lock --check、py_compile、msgfmt --check + .mo比較、pytest(`uv run --frozen --group dev pytest`)、git diff --check。
- ruff: `uv run ruff check src/`(コミット前のローカル検証として実施)。

## セキュリティ

- APIキー等の秘密情報をコード・設定例・コミットに含めない。
- 秘密情報は `.env`(git追跡対象外)または環境変数で管理する。
- `PROJECT.md`・`開発資料/` は `.gitignore` 管理下にあり、リポジトリには公開しない。