#!/usr/bin/env bash
set -euo pipefail
if (( $# != 3 )); then echo 'Usage: upgrade.sh <prepare|copy-config|post-copy|finalize> <current-dir> <target-dir>' >&2; exit 2; fi
phase=$1; current=$2; target=$3

copy_if_missing() {
  local source=$1 destination=$2
  [[ -f "$source" && ! -L "$source" ]] || return 0
  [[ -e "$destination" ]] && return 0
  install -m 600 "$source" "$destination"
}

set_runtime_paths() {
  local file=$1
  local temporary="${file}.tmp.$$"
  jq --arg projects "/opt/deploy/tester/config/projects.json" \
     --arg env_file "/opt/deploy/tester/config/tester.env" \
     'if has("TESTER_CONTROL_CONFIG") then .TESTER_CONTROL_CONFIG = $projects else . end
      | if has("TESTER_ENV_FILE") then .TESTER_ENV_FILE = $env_file else . end' \
     "$file" > "$temporary"
  chmod 600 "$temporary"
  mv -f "$temporary" "$file"
}

update_systemd_runtime_path() {
  [[ "$(id -u)" -eq 0 && -d /run/systemd/system ]] || return 0
  local drop_in_dir=/etc/systemd/system/tester-control.service.d
  local drop_in="$drop_in_dir/20-runtime-config.conf"
  local temporary="${drop_in}.tmp.$$"
  install -d -m 755 "$drop_in_dir"
  printf '[Service]\nEnvironment=TESTER_RUNTIME_CONFIG=/opt/deploy/tester/config/runtime.json\n' > "$temporary"
  chmod 644 "$temporary"
  mv -f "$temporary" "$drop_in"
  systemctl daemon-reload
}

copy_runtime_config() {
  local current_config="$current/config"
  local target_config="$target/config"
  local runtime_source="${TESTER_RUNTIME_CONFIG:-}"

  install -d -m 700 "$current_config" "$target_config"

  if [[ -f "$current_config/runtime.json" && ! -L "$current_config/runtime.json" ]]; then
    runtime_source="$current_config/runtime.json"
  elif [[ -f "$target_config/runtime.json" && ! -L "$target_config/runtime.json" ]]; then
    runtime_source="$target_config/runtime.json"
  elif [[ -z "$runtime_source" ]]; then
    runtime_source=$(systemctl show tester-control.service --property=Environment --value 2>/dev/null \
      | sed -n 's/.*TESTER_RUNTIME_CONFIG=\([^[:space:]]*\).*/\1/p' | head -n 1)
  fi

  if [[ -z "$runtime_source" || ! -f "$runtime_source" || -L "$runtime_source" ]]; then
    echo "Tester runtime configuration is missing" >&2
    return 1
  fi

  copy_if_missing "$runtime_source" "$current_config/runtime.json"
  local runtime_file="$current_config/runtime.json"
  jq -e 'type == "object" and all(.[]; type == "string")' "$runtime_file" >/dev/null

  local projects_source env_source
  projects_source=$(jq -r '.TESTER_CONTROL_CONFIG // empty' "$runtime_file")
  env_source=$(jq -r '.TESTER_ENV_FILE // empty' "$runtime_file")

  if [[ -n "$projects_source" ]]; then
    copy_if_missing "$projects_source" "$current_config/projects.json"
    if [[ ! -f "$current_config/projects.json" ]]; then
      copy_if_missing "$current_config/consolidated-projects.json" "$current_config/projects.json"
    fi
    if [[ ! -f "$current_config/projects.json" ]]; then
      echo "Tester project configuration is missing" >&2
      return 1
    fi
  fi

  if [[ -n "$env_source" ]]; then
    copy_if_missing "$env_source" "$current_config/tester.env"
    copy_if_missing "$current_config/.env.local" "$current_config/tester.env"
    if [[ ! -f "$current_config/tester.env" ]]; then
      echo "Tester E2E environment file is missing" >&2
      return 1
    fi
  fi

  [[ ! -f "$current_config/projects.json" ]] || chmod 600 "$current_config/projects.json"
  [[ ! -f "$current_config/tester.env" ]] || chmod 600 "$current_config/tester.env"
  set_runtime_paths "$runtime_file"
  chmod 600 "$runtime_file"

  copy_if_missing "$current_config/projects.json" "$target_config/projects.json"
  copy_if_missing "$current_config/tester.env" "$target_config/tester.env"
  if [[ ! -e "$target_config/runtime.json" ]]; then
    install -m 600 "$runtime_file" "$target_config/runtime.json"
  else
    jq -e 'type == "object" and all(.[]; type == "string")' "$target_config/runtime.json" >/dev/null
    set_runtime_paths "$target_config/runtime.json"
    chmod 600 "$target_config/runtime.json"
  fi
  update_systemd_runtime_path
}

case "$phase" in
  prepare) test -d "$target/config" ;;
  copy-config) copy_runtime_config ;;
  post-copy|finalize) : ;;
  *) exit 2 ;;
esac
