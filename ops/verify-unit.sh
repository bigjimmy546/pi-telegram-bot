#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

project_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
unit="$project_root/ops/pi-telegram.service"
production_exec='ExecStart=%h/.local/opt/telegram-pi-bot/current/.venv/bin/telegram-pi-bot run --config %h/.config/telegram-pi-bot/config.toml'
launcher=${1:-$project_root/.venv/bin/telegram-pi-bot}

[[ $launcher =~ ^/[A-Za-z0-9._/-]+$ && -x $launcher && ! -L $launcher ]] || {
    printf '%s\n' "Unit verification requires an existing absolute launcher." >&2
    exit 2
}
grep -Fx -- "$production_exec" "$unit" >/dev/null || {
    printf '%s\n' "The production unit has an unexpected ExecStart." >&2
    exit 2
}

probe=$(mktemp --suffix=.service /tmp/pi-telegram-verify.XXXXXXXX)
cleanup() {
    rm -f -- "$probe"
}
trap cleanup EXIT
sed "s|^ExecStart=.*$|ExecStart=$launcher run --config %h/.config/telegram-pi-bot/config.toml|" "$unit" >"$probe"
systemd-analyze --user verify "$probe"
