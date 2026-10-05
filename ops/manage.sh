#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

fail() {
    printf '%s\n' "${1:-Operation failed.}" >&2
    exit 2
}

user_home=${TPB_HOME:-${HOME:-}}
[[ $user_home == /* && $user_home != / ]] || fail "HOME must be an absolute scoped directory."
project_root=${TPB_PROJECT_ROOT:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)}
install_root=${TPB_INSTALL_ROOT:-$user_home/.local/opt/telegram-pi-bot}
config_root=${TPB_CONFIG_ROOT:-$user_home/.config/telegram-pi-bot}
state_root=${TPB_STATE_ROOT:-$user_home/.local/state/telegram-pi-bot}
unit_dir=${TPB_SYSTEMD_USER_DIR:-$user_home/.config/systemd/user}
systemctl_cmd=${TPB_SYSTEMCTL:-systemctl}
loginctl_cmd=${TPB_LOGINCTL:-loginctl}
proc_root=${TPB_PROC_ROOT:-/proc}
stability_seconds=${TPB_STABILITY_SECONDS:-6}
wait_attempts=${TPB_WAIT_ATTEMPTS:-60}
wait_seconds=${TPB_WAIT_SECONDS:-0.1}

service=pi-telegram.service
releases="$install_root/releases"
current="$install_root/current"
config_path="$config_root/config.toml"
secrets_path="$config_root/secrets.env"
unit_path="$unit_dir/$service"
enablement_link="$unit_dir/default.target.wants/$service"
backup="$state_root/deployment-previous"

for path in "$project_root" "$install_root" "$config_root" "$state_root" "$unit_dir" "$proc_root"; do
    [[ $path == /* && $path != / ]] || fail "All operational paths must be absolute and scoped."
done
[[ $stability_seconds =~ ^[0-9]+([.][0-9]+)?$ ]] || fail "Invalid stability interval."
[[ $wait_attempts =~ ^[1-9][0-9]*$ && $wait_seconds =~ ^[0-9]+([.][0-9]+)?$ ]] || fail "Invalid stop interval."

systemctl_user() {
    "$systemctl_cmd" --user "$@"
}

is_active() {
    systemctl_user is-active --quiet "$service" >/dev/null 2>&1
}

enabled_state() {
    local value
    value=$(systemctl_user is-enabled "$service" 2>/dev/null || true)
    case "$value" in
        enabled|disabled|masked|not-found) printf '%s\n' "$value" ;;
        *) return 1 ;;
    esac
}

require_private_file() {
    local path=$1
    [[ -f $path && ! -L $path ]] || return 1
    [[ $(stat -c '%u' -- "$path") == "$(id -u)" ]] || return 1
    [[ $(stat -c '%a' -- "$path") == 600 ]]
}

require_private_directory() {
    local path=$1
    [[ -d $path && ! -L $path ]] || return 1
    [[ $(stat -c '%u' -- "$path") == "$(id -u)" ]] || return 1
    [[ $(stat -c '%a' -- "$path") == 700 ]]
}

ensure_private_directory() {
    local path=$1
    if [[ ! -e $path && ! -L $path ]]; then
        mkdir -p -- "$path" || return 1
        chmod 0700 -- "$path" || return 1
    fi
    require_private_directory "$path"
}

release_path() {
    local build_id=$1
    [[ $build_id =~ ^[0-9a-f]{40}$ ]] || return 1
    printf '%s/%s\n' "$releases" "$build_id"
}

verify_release() {
    local build_id=$1
    local release
    release=$(release_path "$build_id") || return 1
    [[ -d $release && ! -L $release ]] || return 1
    [[ -f $release/SOURCE_COMMIT && $(<"$release/SOURCE_COMMIT") == "$build_id" ]] || return 1
    [[ -x $release/.venv/bin/telegram-pi-bot && -f $release/ops/pi-telegram.service ]] || return 1
    (cd -- "$release" && sha256sum --check --quiet SHA256SUMS) || return 1
    printf '%s\n' "$release"
}

read_current() {
    if [[ -L $current ]]; then
        readlink -- "$current"
    elif [[ -e $current ]]; then
        return 1
    else
        printf '%s\n' '__ABSENT__'
    fi
}

atomic_link() {
    local target=$1
    local staged="$install_root/.current.$$"
    [[ ! -e $staged && ! -L $staged ]] || return 1
    ln -s -- "$target" "$staged" || return 1
    mv -Tf -- "$staged" "$current" || return 1
}

atomic_unit() {
    local source=$1
    local staged
    staged=$(mktemp "$unit_dir/.pi-telegram.service.XXXXXXXX") || return 1
    install -m 0644 -- "$source" "$staged" || return 1
    mv -Tf -- "$staged" "$unit_path" || return 1
}

capture_snapshot() {
    local destination=$1
    local unit_kind=absent
    [[ -d $destination && ! -L $destination ]] || return 1
    chmod 0700 -- "$destination" || return 1
    read_current >"$destination/previous-target" || return 1
    enabled_state >"$destination/previous-enabled" || return 1
    if is_active; then
        printf '%s\n' active >"$destination/previous-active" || return 1
    else
        printf '%s\n' inactive >"$destination/previous-active" || return 1
    fi
    if [[ -L $unit_path ]]; then
        unit_kind=symlink
        readlink -- "$unit_path" >"$destination/previous-unit-link" || return 1
    elif [[ -f $unit_path ]]; then
        unit_kind=regular
        cp -p -- "$unit_path" "$destination/previous-unit" || return 1
    elif [[ -e $unit_path ]]; then
        return 1
    fi
    printf '%s\n' "$unit_kind" >"$destination/previous-unit-kind" || return 1
    return 0
}

atomic_restore_unit() {
    local source=$1
    local staged
    [[ -f $source && ! -L $source ]] || return 1
    staged=$(mktemp "$unit_dir/.pi-telegram.service.restore.XXXXXXXX") || return 1
    cp -p -- "$source" "$staged" || return 1
    mv -Tf -- "$staged" "$unit_path" || return 1
}

clear_deployed_enablement() {
    if [[ -e $unit_path || -L $unit_path ]]; then
        systemctl_user disable "$service" >/dev/null || return 1
    fi
    if [[ -L $enablement_link ]]; then
        unlink -- "$enablement_link" || return 1
    elif [[ -e $enablement_link ]]; then
        return 1
    fi
}

restore_snapshot() {
    local source=$1
    local target enabled active kind staged stopped_pid restored_pid launcher
    [[ -d $source && ! -L $source ]] || return 1
    target=$(<"$source/previous-target")
    enabled=$(<"$source/previous-enabled")
    active=$(<"$source/previous-active")
    kind=$(<"$source/previous-unit-kind")

    stopped_pid=$(systemctl_user show "$service" -p MainPID --value 2>/dev/null || printf '0')
    [[ $stopped_pid =~ ^[0-9]+$ ]] || return 1
    systemctl_user stop "$service" >/dev/null 2>&1 || true
    wait_gone "$stopped_pid" || return 1
    clear_deployed_enablement || return 1
    if [[ $target == __ABSENT__ ]]; then
        [[ ! -L $current ]] || unlink -- "$current" || return 1
        [[ ! -e $current ]] || return 1
    else
        atomic_link "$target" || return 1
    fi

    case "$kind" in
        absent)
            [[ ! -L $unit_path ]] || unlink -- "$unit_path" || return 1
            [[ ! -e $unit_path ]] || rm -f -- "$unit_path" || return 1
            ;;
        regular)
            atomic_restore_unit "$source/previous-unit" || return 1
            ;;
        symlink)
            staged="$unit_dir/.pi-telegram.service.restore.$$"
            [[ ! -e $staged && ! -L $staged ]] || return 1
            ln -s -- "$(<"$source/previous-unit-link")" "$staged" || return 1
            mv -Tf -- "$staged" "$unit_path" || return 1
            ;;
        *) return 1 ;;
    esac
    systemctl_user daemon-reload >/dev/null || return 1
    case "$enabled" in
        enabled) systemctl_user enable "$service" >/dev/null || return 1 ;;
        disabled|masked|not-found) ;;
        *) return 1 ;;
    esac
    case "$active" in
        active)
            systemctl_user start "$service" >/dev/null || return 1
            launcher="$current/.venv/bin/telegram-pi-bot"
            restored_pid=$(wait_pid_identity "$launcher") || return 1
            ;;
        inactive)
            systemctl_user stop "$service" >/dev/null 2>&1 || true
            if is_active; then
                return 1
            fi
            ;;
        *) return 1 ;;
    esac
    return 0
}

wait_gone() {
    local pid=$1
    local attempt
    for ((attempt = 0; attempt < wait_attempts; attempt++)); do
        if ! is_active && { [[ $pid == 0 ]] || [[ ! -e $proc_root/$pid ]]; }; then
            return 0
        fi
        sleep "$wait_seconds"
    done
    return 1
}

verify_pid_identity() {
    local pid=$1
    local launcher=$2
    local offset
    local -a arguments=()
    [[ $pid =~ ^[1-9][0-9]*$ && -r $proc_root/$pid/cmdline ]] || return 1
    mapfile -d '' -t arguments <"$proc_root/$pid/cmdline"
    if [[ ${arguments[0]:-} == "$launcher" ]]; then
        offset=0
    elif [[ ${arguments[1]:-} == "$launcher" ]]; then
        offset=1
    else
        return 1
    fi
    [[ ${#arguments[@]} -eq $((offset + 4)) ]] || return 1
    [[ ${arguments[offset + 1]:-} == run
        && ${arguments[offset + 2]:-} == --config
        && ${arguments[offset + 3]:-} == "$config_path" ]]
}

wait_pid_identity() {
    local launcher=$1
    local attempt pid
    for ((attempt = 0; attempt < wait_attempts; attempt++)); do
        if is_active; then
            pid=$(systemctl_user show "$service" -p MainPID --value 2>/dev/null || printf '0')
            if verify_pid_identity "$pid" "$launcher"; then
                printf '%s\n' "$pid"
                return 0
            fi
        fi
        sleep "$wait_seconds"
    done
    return 1
}

run_doctor() {
    local release=$1
    local expected_pid=${2:-}
    local -a arguments=(doctor --config "$config_path")
    if [[ -n $expected_pid ]]; then
        arguments+=(--expected-poller-pid "$expected_pid")
    fi
    PYTHONDONTWRITEBYTECODE=1 "$release/.venv/bin/telegram-pi-bot" "${arguments[@]}"
}

status() {
    local target active enabled pid
    target=$(read_current) || fail "Current release path is not a symlink."
    active=$(systemctl_user is-active "$service" 2>/dev/null || true)
    enabled=$(enabled_state 2>/dev/null || printf '%s' unknown)
    pid=$(systemctl_user show "$service" -p MainPID --value 2>/dev/null || printf '0')
    printf 'Current release: %s\nService: %s\nEnabled: %s\nPID: %s\n' "$target" "${active:-unknown}" "$enabled" "${pid:-0}"
}

deploy() {
    local build_id=$1
    local release launcher previous_target old_pid new_pid stable_pid snapshot old_backup
    release=$(verify_release "$build_id") || fail "Release is missing or failed checksum verification."
    launcher="$current/.venv/bin/telegram-pi-bot"
    require_private_directory "$config_root" || fail "Configuration directory must be owned and mode 0700."
    for path in "$state_root" "$state_root/attachments" "$state_root/artifacts" "$state_root/artifacts/staging"; do
        ensure_private_directory "$path" || fail "State directories must be owned and mode 0700."
    done
    require_private_file "$config_path" || fail "config.toml must be owned and mode 0600."
    require_private_file "$secrets_path" || fail "secrets.env must be owned and mode 0600."
    [[ $("$loginctl_cmd" show-user "$(id -un)" -p Linger --value) == yes ]] || fail "User lingering must already be enabled."
    mkdir -p -- "$install_root" "$unit_dir"
    chmod 0700 -- "$install_root"
    exec 9>"$state_root/deployment.lock"
    flock -n 9 || fail "Another deployment operation is active."

    previous_target=$(read_current) || fail "Current release path is not a symlink."
    if is_active && [[ $previous_target == __ABSENT__ ]]; then
        fail "Refusing to stop an untracked active poller."
    fi
    old_pid=$(systemctl_user show "$service" -p MainPID --value 2>/dev/null || printf '0')
    [[ $old_pid =~ ^[0-9]+$ ]] || fail "The active service PID is invalid."
    if is_active; then
        verify_pid_identity "$old_pid" "$launcher" || fail "Refusing to stop an active process with unexpected identity."
    fi

    snapshot=$(mktemp -d "$state_root/.deployment.XXXXXXXX")
    capture_snapshot "$snapshot" || fail "Could not record prior deployment state."
    old_backup=

    recover_deploy() {
        local code=$? restore_code backup_code=0
        trap - ERR INT TERM HUP
        set +e
        restore_snapshot "$snapshot"
        restore_code=$?
        if [[ -n $old_backup && -d $old_backup && ! -e $backup ]]; then
            mv -T -- "$old_backup" "$backup" || backup_code=$?
        fi
        if ((restore_code != 0 || backup_code != 0)); then
            printf 'Deployment failed and prior state could not be fully restored; recovery snapshot retained at %s.\n' "$snapshot" >&2
            exit 3
        fi
        rm -rf -- "$snapshot" 2>/dev/null || true
        printf '%s\n' "Deployment failed; prior target, unit, enablement, and activity were restored." >&2
        exit "${code:-1}"
    }
    trap recover_deploy ERR
    trap 'false' INT TERM HUP

    systemctl_user stop "$service" >/dev/null 2>&1 || true
    wait_gone "$old_pid"
    run_doctor "$release"

    atomic_link "$release"
    atomic_unit "$release/ops/pi-telegram.service"
    systemctl_user daemon-reload >/dev/null
    systemctl_user enable --now "$service" >/dev/null
    new_pid=$(trap - ERR; wait_pid_identity "$launcher")
    sleep "$stability_seconds"
    is_active
    stable_pid=$(trap - ERR; systemctl_user show "$service" -p MainPID --value)
    [[ $stable_pid == "$new_pid" ]]
    run_doctor "$release" "$new_pid"

    printf '%s\n' "$release" >"$snapshot/activated-target"
    if [[ -e $backup || -L $backup ]]; then
        old_backup="$state_root/.deployment-previous.$$"
        [[ ! -e $old_backup && ! -L $old_backup ]]
        mv -T -- "$backup" "$old_backup"
    fi
    mv -T -- "$snapshot" "$backup"
    snapshot=
    trap - ERR INT TERM HUP
    if [[ -n $old_backup ]]; then
        rm -rf -- "$old_backup"
    fi
    printf 'Deployed release %s with one healthy poller (PID %s).\n' "$build_id" "$new_pid"
}

rollback() {
    local activated target current_pid launcher
    require_private_directory "$state_root" || fail "State directory must be owned and mode 0700."
    [[ -d $backup && ! -L $backup ]] || fail "No deployment rollback record is available."
    exec 9>"$state_root/deployment.lock"
    flock -n 9 || fail "Another deployment operation is active."
    activated=$(<"$backup/activated-target")
    target=$(read_current) || fail "Current release path is not a symlink."
    [[ $target == "$activated" ]] || fail "Rollback record does not match the active release."
    if is_active; then
        current_pid=$(systemctl_user show "$service" -p MainPID --value 2>/dev/null || printf '0')
        launcher="$current/.venv/bin/telegram-pi-bot"
        verify_pid_identity "$current_pid" "$launcher" || fail "Refusing to stop an active process with unexpected identity."
    fi
    restore_snapshot "$backup" || fail "Rollback could not restore the prior state."
    printf '%s\n' "Previous target, unit, enablement, and activity restored; releases and Pi sessions were left intact."
}

usage() {
    printf 'Usage: %s status | deploy <40-hex-build-id> | rollback\n' "$0" >&2
    exit 2
}

[[ $# -ge 1 ]] || usage
case "$1" in
    status)
        [[ $# -eq 1 ]] || usage
        status
        ;;
    deploy)
        [[ $# -eq 2 ]] || usage
        deploy "$2"
        ;;
    rollback)
        [[ $# -eq 1 ]] || usage
        rollback
        ;;
    *) usage ;;
esac
