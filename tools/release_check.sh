#!/usr/bin/env bash
# Release pre-flight check for a given version.
#
# Usage: tools/release_check.sh <version>
#
# Verifies that every version declaration matches <version>, runs
# tools/check.sh, confirms a clean pushed tree, prints release evidence
# (branch, tags, merge state), and confirms the release tag is not yet
# created. Exits 1 if any check fails.

set -uo pipefail

cd "$(dirname "$0")/.."

if [[ $# -ne 1 ]]; then
    echo "Usage: $0 <version>" >&2
    exit 1
fi

version="$1"
failures=0

ok() { echo "[OK] $1"; }
ng() { echo "[NG] $1"; failures=$((failures + 1)); }

check() { # check <label> <command...>
    local label="$1"
    shift
    if "$@"; then
        ok "$label"
    else
        ng "$label"
    fi
}

check_pyproject() {
    grep -q "^version = \"$version\"$" pyproject.toml
}

check_uv_lock() {
    grep -A1 '^name = "niche-llm-proxy"$' uv.lock | grep -q "^version = \"$version\"$"
}

check_app_version() {
    grep -q "^PROXY_VERSION = \"$version\"$" src/niche_llm_proxy/app.py
}

check_docker_tags() { # check_docker_tags <file>
    local tag found=0
    while IFS= read -r tag; do
        [[ "$tag" == "nichellm-proxy:$version" ]] || return 1
        found=1
    done < <(grep -oh 'nichellm-proxy:[0-9][0-9.]*' "$1" | sort -u)
    return $((1 - found))
}

check_changelog_entry() { # check_changelog_entry <file>
    local heading
    heading="$(grep -m1 '^### v' "$1")"
    [[ -z "$heading" ]] && return 1
    heading="${heading#\#\#\# }"
    heading="${heading#v}"
    [[ "$heading" == "$version "* || "$heading" == "$version"*"("* ]]
}

check_clean_tree() {
    [[ -z "$(git status --porcelain --untracked-files=no)" ]]
}

check_pushed() {
    local upstream
    upstream="$(git rev-parse --abbrev-ref --symbolic-full-name '@{upstream}' 2>/dev/null)" || return 1
    [[ -z "$(git log "$upstream"..HEAD)" ]]
}

echo "== version declarations =="
check "pyproject.toml version = $version" check_pyproject
check "uv.lock niche-llm-proxy version = $version" check_uv_lock
check "app.py PROXY_VERSION = $version" check_app_version
check "README.md Docker Hub tag examples = $version" check_docker_tags README.md
check "README_ja.md Docker Hub tag examples = $version" check_docker_tags README_ja.md
check "README.md latest changelog entry = v$version" check_changelog_entry README.md
check "README_ja.md latest changelog entry = v$version" check_changelog_entry README_ja.md
check "CHANGELOG.md latest entry = v$version" check_changelog_entry CHANGELOG.md

echo "== tools/check.sh =="
check_log="$(mktemp)"
trap 'rm -f "$check_log"' EXIT
if bash tools/check.sh >"$check_log" 2>&1; then
    ok "tools/check.sh passed"
else
    ng "tools/check.sh passed"
    sed 's/^/    /' "$check_log"
fi

echo "== clean tree =="
check "no uncommitted changes" check_clean_tree
check "all commits pushed" check_pushed

echo "== release evidence =="
echo "branch: $(git branch --show-current)"
echo "local tags:"
git tag --list 'v*' | sed 's/^/  /'
remote_reachable=1
if ! remote_tags="$(git ls-remote --tags origin 'refs/tags/v*' 2>/dev/null)"; then
    remote_reachable=0
fi
if [[ $remote_reachable -eq 1 ]]; then
    echo "remote tags (origin):"
    echo "$remote_tags" | grep -v '\^{}$' | sed 's|.*refs/tags/|  |'
else
    echo "remote tags (origin): unreachable"
fi
if git merge-base --is-ancestor HEAD main 2>/dev/null; then
    echo "merged to main: yes"
else
    echo "merged to main: no"
fi
if git rev-parse -q --verify "refs/tags/v$version" >/dev/null 2>&1; then
    ng "tag v$version not created locally"
else
    ok "tag v$version not created locally"
fi
if [[ $remote_reachable -eq 1 ]]; then
    if echo "$remote_tags" | grep -q "refs/tags/v$version$"; then
        ng "tag v$version not created on origin"
    else
        ok "tag v$version not created on origin"
    fi
else
    echo "[??] tag v$version on origin: unverified (origin unreachable)"
fi

if [[ $failures -eq 0 ]]; then
    echo "All checks passed."
    exit 0
else
    echo "$failures check(s) failed."
    exit 1
fi