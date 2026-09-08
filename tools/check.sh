#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

echo "== uv lock --check =="
uv lock --check

echo "== py_compile =="
uv run python -m py_compile src/niche_llm_proxy/*.py

echo "== msgfmt =="
tmp_mo="$(mktemp)"
trap 'rm -f "$tmp_mo"' EXIT
msgfmt --check --output-file "$tmp_mo" src/niche_llm_proxy/locales/ja/LC_MESSAGES/niche_llm_proxy.po
if ! cmp --silent "$tmp_mo" src/niche_llm_proxy/locales/ja/LC_MESSAGES/niche_llm_proxy.mo; then
    echo "ja catalog .mo is out of date. Regenerate with:" >&2
    echo "  msgfmt --check --output-file src/niche_llm_proxy/locales/ja/LC_MESSAGES/niche_llm_proxy.mo src/niche_llm_proxy/locales/ja/LC_MESSAGES/niche_llm_proxy.po" >&2
    exit 1
fi

echo "== pytest =="
uv run --frozen --group dev pytest

echo "== git diff --check =="
git diff --check HEAD

echo "All checks passed."