#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

fail() {
    printf '%s\n' "${1:-Release installation failed.}" >&2
    exit 2
}

user_home=${TPB_HOME:-${HOME:-}}
[[ $user_home == /* && $user_home != / ]] || fail "HOME must be an absolute scoped directory."
project_root=${TPB_PROJECT_ROOT:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)}
install_root=${TPB_INSTALL_ROOT:-$user_home/.local/opt/telegram-pi-bot}
config_root=${TPB_CONFIG_ROOT:-$user_home/.config/telegram-pi-bot}
state_root=${TPB_STATE_ROOT:-$user_home/.local/state/telegram-pi-bot}
unit_dir=${TPB_SYSTEMD_USER_DIR:-$user_home/.config/systemd/user}
release_root="$install_root/releases"

[[ $# -eq 1 && $1 =~ ^[0-9a-f]{40}$ ]] || fail "Usage: $0 <40-hex-commit>"
build_id=$1
[[ $project_root == /* && $install_root == /* && $install_root != / ]] || fail "Release paths must be absolute and scoped."

cd -- "$project_root"
[[ -z $(git status --porcelain --untracked-files=all) ]] || fail "Refusing to package a dirty worktree."
[[ $(git rev-parse --verify HEAD) == "$build_id" ]] || fail "Build ID must equal the checked-out commit."

uv lock --check
uv sync --frozen
uv run python -m compileall -q src tests
uv run python -m unittest discover -s tests -v
uv run python tests/live_runtime_probe.py --metadata
uv run python tests/live_runtime_probe.py --local-text
ops/verify-unit.sh
git diff --check

mkdir -p -- "$release_root"
chmod 0700 -- "$install_root" "$release_root"
exec 9>"$install_root/install.lock"
flock -n 9 || fail "Another release installation is active."

destination="$release_root/$build_id"
verify_release() {
    local release=$1
    [[ -d $release && ! -L $release ]] || return 1
    [[ $(<"$release/SOURCE_COMMIT") == "$build_id" ]] || return 1
    (cd -- "$release" && sha256sum --check --quiet SHA256SUMS)
}

if [[ -e $destination || -L $destination ]]; then
    verify_release "$destination" || fail "Existing release does not match its manifest."
    printf 'Verified existing inactive release: %s\n' "$destination"
    exit 0
fi

stage=$(mktemp -d "$release_root/.stage.XXXXXXXX")
cleanup() {
    if [[ -n ${stage:-} && -d $stage ]]; then
        chmod -R u+w -- "$stage" 2>/dev/null || true
        rm -rf -- "$stage"
    fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

archive_paths=(
    src
    ops
    docs
    config
    pyproject.toml
    uv.lock
    README.md
    SPEC.md
    LICENSE
)
git archive "$build_id" -- "${archive_paths[@]}" | tar -x -C "$stage"
printf '%s\n' "$build_id" >"$stage/SOURCE_COMMIT"

(
    cd -- "$stage"
    uv venv --python 3.13 --relocatable .venv
    VIRTUAL_ENV="$stage/.venv" uv sync --frozen --no-dev --no-editable --active --link-mode copy
    extension=.venv/lib/python3.13/site-packages/telegram_pi_bot/extensions/telegram_artifacts.ts
    [[ -f $extension && ! -L $extension ]] || fail "The packaged artifact extension is missing."
    PYTHONDONTWRITEBYTECODE=1 .venv/bin/telegram-pi-bot --help >/dev/null
    ops/verify-unit.sh "$stage/.venv/bin/telegram-pi-bot"
    find . -type f ! -name SHA256SUMS -print0 \
        | LC_ALL=C sort -z \
        | xargs -0 sha256sum >SHA256SUMS
    sha256sum --check --quiet SHA256SUMS
)

chmod -R a-w -- "$stage"
mv -T -- "$stage" "$destination"
stage=
printf 'Installed inactive release: %s\n' "$destination"
