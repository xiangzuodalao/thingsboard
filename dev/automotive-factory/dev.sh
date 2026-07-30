#!/usr/bin/env bash
#
# Copyright © 2016-2026 The Thingsboard Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

set -Eeuo pipefail
umask 077

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/../.." && pwd -P)"
COMPOSE_FILE="${SCRIPT_DIR}/docker-compose.yml"
ENV_FILE="${SCRIPT_DIR}/.env"
ENV_EXAMPLE="${SCRIPT_DIR}/.env.example"
CONFIG_FILE="${SCRIPT_DIR}/config.yml"
REQUIREMENTS_FILE="${SCRIPT_DIR}/requirements.txt"
VENV_DIR="${SCRIPT_DIR}/.venv"
PYTHON_BIN="${VENV_DIR}/bin/python"
RUNTIME_DIR="${SCRIPT_DIR}/.runtime"
LOG_DIR="${RUNTIME_DIR}/logs"
BACKEND_PID_FILE="${RUNTIME_DIR}/backend.pid"
UI_PID_FILE="${RUNTIME_DIR}/ui.pid"
SIMULATOR_PID_FILE="${RUNTIME_DIR}/simulator.pid"
SIMULATOR_LAUNCHER_PID_FILE="${RUNTIME_DIR}/simulator-launcher.pid"
BACKEND_LOG="${LOG_DIR}/backend.log"
UI_LOG="${LOG_DIR}/ui.log"
SIMULATOR_LOG="${LOG_DIR}/simulator.log"
LOGROTATE_CONFIG="${RUNTIME_DIR}/logrotate.conf"
LOGROTATE_STATE="${RUNTIME_DIR}/logrotate.status"
APP_TARGET="${REPO_ROOT}/application/target"
UI_DIR="${REPO_ROOT}/ui-ngx"
BACKEND_PORT=8080
UI_PORT=4200
MQTT_PORT=1883
DB_VOLUME_NAME="tb-automotive-postgres-data"
COMPOSE_PROJECT_NAME="tb-automotive-factory"
DOCKER_CMD=()
BACKEND_SYSTEMD_UNIT="tb-automotive-backend"
UI_SYSTEMD_UNIT="tb-automotive-ui"
SIMULATOR_SYSTEMD_UNIT="tb-automotive-simulator"
LOGROTATE_SYSTEMD_UNIT="tb-automotive-logrotate"
LAUNCHED_PID=""
SYSTEMD_ENV_ARGS=()

on_error() {
    local exit_code=$?
    trap - ERR
    printf '错误：命令在 dev.sh 第 %s 行失败（退出码 %s）。\n' \
        "${BASH_LINENO[0]:-unknown}" "${exit_code}" >&2
    exit "${exit_code}"
}
trap on_error ERR

log() {
    printf '[automotive-factory] %s\n' "$*"
}

warn() {
    printf '[automotive-factory] 警告：%s\n' "$*" >&2
}

die() {
    printf '[automotive-factory] 错误：%s\n' "$*" >&2
    exit 1
}

usage() {
    cat <<'EOF'
用法：
  ./dev.sh prepare
  ./dev.sh db-up
  ./dev.sh build
  ./dev.sh db-init
  ./dev.sh backend-start
  ./dev.sh ui-start
  ./dev.sh provision
  ./dev.sh dashboard
  ./dev.sh dashboard-plan [--actor ACTOR]
  ./dev.sh dashboard-apply --plan-sha256 SHA256 --confirm-sha256 SHA256
  ./dev.sh sim-start
  ./dev.sh up
  ./dev.sh down
  ./dev.sh status
  ./dev.sh logs [backend|ui|sim|db|all] [-f]
  ./dev.sh log-rotate
  ./dev.sh log-rotation-install
  ./dev.sh fault DEVICE FAULT [--duration SECONDS] [--replace]
  ./dev.sh clear DEVICE
  ./dev.sh verify
  ./dev.sh reset --yes

常用示例：
  ./dev.sh up
  ./dev.sh fault LINE-A-CNC-01 HIGH_TEMPERATURE --duration 60
  ./dev.sh clear LINE-A-CNC-01
  ./dev.sh logs sim -f
EOF
}

ensure_runtime_layout() {
    mkdir -p -- "${RUNTIME_DIR}" "${LOG_DIR}"
}

write_logrotate_config() {
    local rotate_size="${TB_LOG_ROTATE_SIZE:-10M}"
    local rotate_count="${TB_LOG_ROTATE_COUNT:-2}"
    [[ "${rotate_size}" =~ ^[1-9][0-9]*([kMG])?$ ]] ||
        die "TB_LOG_ROTATE_SIZE 必须是正整数，可带 k、M 或 G 后缀。"
    [[ "${rotate_count}" =~ ^[1-9][0-9]*$ ]] ||
        die "TB_LOG_ROTATE_COUNT 必须是正整数。"

    cat >"${LOGROTATE_CONFIG}" <<EOF
${BACKEND_LOG} ${UI_LOG} ${SIMULATOR_LOG} {
    size ${rotate_size}
    rotate ${rotate_count}
    compress
    copytruncate
    dateext
    dateformat -%Y%m%d-%H%M%S
    missingok
    notifempty
}
EOF
}

run_log_rotate() {
    ensure_runtime_layout
    load_env_if_present
    require_command logrotate "请安装 logrotate。"
    write_logrotate_config
    logrotate --state "${LOGROTATE_STATE}" "${LOGROTATE_CONFIG}"
}

run_log_rotation_install() {
    ensure_runtime_layout
    require_command logrotate "请安装 logrotate。"
    if ! user_systemd_available; then
        warn "用户级 systemd 不可用，无法安装日志轮转定时器；可手动定期运行 ./dev.sh log-rotate。"
        return 0
    fi
    [[ -n "${HOME:-}" ]] ||
        die "HOME 未设置，无法定位用户级 systemd 配置目录。"

    local systemd_config_root="${XDG_CONFIG_HOME:-${HOME}/.config}"
    local user_unit_dir="${systemd_config_root}/systemd/user"
    local service_file="${user_unit_dir}/${LOGROTATE_SYSTEMD_UNIT}.service"
    local timer_file="${user_unit_dir}/${LOGROTATE_SYSTEMD_UNIT}.timer"
    mkdir -p -- "${user_unit_dir}"

    cat >"${service_file}" <<EOF
[Unit]
Description=Rotate ThingsBoard automotive development logs

[Service]
Type=oneshot
ExecStart=${SCRIPT_DIR}/dev.sh log-rotate
Nice=10
IOSchedulingClass=idle
EOF
    cat >"${timer_file}" <<EOF
[Unit]
Description=Rotate ThingsBoard automotive development logs every minute

[Timer]
OnBootSec=30s
OnUnitInactiveSec=1min
AccuracySec=5s
Unit=${LOGROTATE_SYSTEMD_UNIT}.service

[Install]
WantedBy=timers.target
EOF
    chmod 600 "${service_file}" "${timer_file}"
    systemctl --user daemon-reload
    systemctl --user enable --now "${LOGROTATE_SYSTEMD_UNIT}.timer" >/dev/null
    run_log_rotate
    log "日志轮转已启用：单个日志达到 ${TB_LOG_ROTATE_SIZE:-10M} 时轮转，保留 ${TB_LOG_ROTATE_COUNT:-2} 份。"
}

compose_env_file() {
    if [[ -f "${ENV_FILE}" ]]; then
        printf '%s\n' "${ENV_FILE}"
    else
        printf '%s\n' "${ENV_EXAMPLE}"
    fi
}

load_env() {
    [[ -f "${ENV_FILE}" ]] ||
        die "缺少 ${ENV_FILE}；请先运行 ./dev.sh prepare。"

    set -a
    # shellcheck disable=SC1090
    source "${ENV_FILE}"
    set +a

    local required_name
    for required_name in \
        TB_TENANT_USERNAME \
        TB_TENANT_PASSWORD \
        TB_POSTGRES_DB \
        TB_POSTGRES_USER \
        TB_POSTGRES_PASSWORD \
        TB_POSTGRES_PORT; do
        [[ -n "${!required_name:-}" ]] ||
            die "${ENV_FILE} 中缺少 ${required_name}。"
    done
    [[ "${TB_POSTGRES_PORT}" =~ ^[0-9]+$ ]] ||
        die "TB_POSTGRES_PORT 必须是数字。"
}

load_env_if_present() {
    if [[ -f "${ENV_FILE}" ]]; then
        load_env
    elif [[ -f "${ENV_EXAMPLE}" ]]; then
        set -a
        # shellcheck disable=SC1090
        source "${ENV_EXAMPLE}"
        set +a
    fi
}

configure_datasource() {
    export SPRING_DATASOURCE_URL="${SPRING_DATASOURCE_URL:-jdbc:postgresql://127.0.0.1:${TB_POSTGRES_PORT}/${TB_POSTGRES_DB}}"
    export SPRING_DATASOURCE_USERNAME="${SPRING_DATASOURCE_USERNAME:-${TB_POSTGRES_USER}}"
    export SPRING_DATASOURCE_PASSWORD="${SPRING_DATASOURCE_PASSWORD:-${TB_POSTGRES_PASSWORD}}"
    export DATABASE_TS_TYPE="${DATABASE_TS_TYPE:-sql}"
    export DATABASE_TS_LATEST_TYPE="${DATABASE_TS_LATEST_TYPE:-sql}"
    export HTTP_BIND_ADDRESS="${HTTP_BIND_ADDRESS:-127.0.0.1}"
    export MQTT_BIND_ADDRESS="${MQTT_BIND_ADDRESS:-127.0.0.1}"
    export COAP_ENABLED="${COAP_ENABLED:-false}"
    export COAP_SERVER_ENABLED="${COAP_SERVER_ENABLED:-false}"
    export LWM2M_ENABLED="${LWM2M_ENABLED:-false}"
    export LWM2M_ENABLED_BS="${LWM2M_ENABLED_BS:-false}"
    export SNMP_ENABLED="${SNMP_ENABLED:-false}"
    export EDGES_ENABLED="${EDGES_ENABLED:-false}"
    export TB_GATEWAY_DASHBOARD_SYNC_ENABLED="${TB_GATEWAY_DASHBOARD_SYNC_ENABLED:-false}"
    export SQL_TTL_TS_ENABLED="${SQL_TTL_TS_ENABLED:-true}"
    export SQL_TTL_TS_TS_KEY_VALUE_TTL="${SQL_TTL_TS_TS_KEY_VALUE_TTL:-604800}"
    [[ "${SQL_TTL_TS_ENABLED}" == "true" ||
        "${SQL_TTL_TS_ENABLED}" == "false" ]] ||
        die "SQL_TTL_TS_ENABLED 必须是 true 或 false。"
    [[ "${SQL_TTL_TS_TS_KEY_VALUE_TTL}" =~ ^[0-9]+$ ]] ||
        die "SQL_TTL_TS_TS_KEY_VALUE_TTL 必须是非负整数秒。"
}

require_command() {
    command -v "$1" >/dev/null 2>&1 ||
        die "未找到命令 '$1'。${2:-}"
}

select_docker_command() {
    if ((${#DOCKER_CMD[@]} > 0)); then
        return 0
    fi
    command -v docker >/dev/null 2>&1 || return 1

    if docker info >/dev/null 2>&1; then
        DOCKER_CMD=(docker)
        return 0
    fi
    command -v sudo >/dev/null 2>&1 || return 1

    if sudo -n docker info >/dev/null 2>&1; then
        DOCKER_CMD=(sudo docker)
        return 0
    fi

    warn "当前用户无法访问 Docker daemon；接下来的 Docker 操作需要 sudo 授权。"
    if sudo docker info >/dev/null; then
        DOCKER_CMD=(sudo docker)
        return 0
    fi
    return 1
}

docker_cmd() {
    if ((${#DOCKER_CMD[@]} == 0)); then
        select_docker_command ||
            die "无法访问 Docker daemon；请启动 Docker，并配置当前用户权限或 sudo 权限。"
    fi
    "${DOCKER_CMD[@]}" "$@"
}

require_docker_compose() {
    require_command docker "请先安装并启动 Docker。"
    select_docker_command ||
        die "无法访问 Docker daemon；请启动 Docker，并配置当前用户权限或 sudo 权限。"
    docker_cmd compose version >/dev/null 2>&1 ||
        die "需要 Docker Compose v2（docker compose）。"
}

compose() {
    docker_cmd compose \
        --project-name "${COMPOSE_PROJECT_NAME}" \
        --project-directory "${SCRIPT_DIR}" \
        --env-file "$(compose_env_file)" \
        --file "${COMPOSE_FILE}" \
        "$@"
}

validate_java() {
    require_command java "本项目需要 JDK 25。"
    local java_output java_version java_major
    java_output="$(java -version 2>&1)"
    java_version="$(sed -n '1s/.*version "\([^"]*\)".*/\1/p' <<<"${java_output}")"
    java_major="${java_version%%.*}"
    [[ "${java_major}" == "25" ]] ||
        die "当前 Java 为 ${java_version:-未知版本}，本项目需要 JDK 25。"
}

validate_maven() {
    require_command mvn "请先安装 Maven。"
    local maven_output
    maven_output="$(mvn -version 2>&1)"
    grep -Eq 'Java version: 25([., ]|$)' <<<"${maven_output}" ||
        die "Maven 没有使用 JDK 25；请检查 JAVA_HOME 和 PATH。"
}

find_boot_jar() {
    local jars=()
    while IFS= read -r -d '' jar; do
        jars+=("${jar}")
    done < <(find "${APP_TARGET}" -maxdepth 1 -type f \
        -name 'thingsboard-*-boot.jar' -print0 2>/dev/null | sort -z)

    case "${#jars[@]}" in
        1)
            printf '%s\n' "${jars[0]}"
            ;;
        0)
            die "未找到 application/target/thingsboard-*-boot.jar；请先运行 ./dev.sh build。"
            ;;
        *)
            die "发现多个 ThingsBoard boot jar；请运行 ./dev.sh build 清理并重新构建。"
            ;;
    esac
}

read_pid() {
    local pid_file=$1
    [[ -f "${pid_file}" ]] || return 1
    local pid
    pid="$(tr -d '[:space:]' <"${pid_file}")"
    [[ "${pid}" =~ ^[1-9][0-9]*$ ]] || return 1
    printf '%s\n' "${pid}"
}

pid_alive() {
    kill -0 "$1" >/dev/null 2>&1
}

process_matches() {
    local pid=$1
    local process_type=$2
    local args=()
    local executable=""
    [[ -r "/proc/${pid}/cmdline" ]] || return 1
    executable="$(readlink -f "/proc/${pid}/exe" 2>/dev/null || true)"
    mapfile -d '' -t args <"/proc/${pid}/cmdline"
    ((${#args[@]} > 0)) || return 1

    local index
    case "${process_type}" in
        backend)
            [[ "${args[0]##*/}" == "java" ]] || return 1
            for ((index = 1; index + 1 < ${#args[@]}; index++)); do
                if [[ "${args[index]}" == "-jar" &&
                    "${args[index + 1]}" == "${APP_TARGET}"/thingsboard-*-boot.jar ]]; then
                    return 0
                fi
            done
            return 1
            ;;
        ui)
            [[ "${executable}" == "${UI_DIR}/target/node/node" ]] || return 1
            if [[ "${args[0]}" == "ng serve"* ]]; then
                return 0
            fi
            for ((index = 1; index + 1 < ${#args[@]}; index++)); do
                if [[ ("${args[index]}" == "${UI_DIR}/node_modules/@angular/cli/bin/ng.js" ||
                    "${args[index]}" == "${UI_DIR}/node_modules/@angular/cli/bin/ng") &&
                    "${args[index + 1]}" == "serve" ]]; then
                    return 0
                fi
            done
            return 1
            ;;
        simulator)
            [[ "${args[0]}" == "${PYTHON_BIN}" &&
                "${args[1]:-}" == "-m" &&
                "${args[2]:-}" == "factory_simulator" &&
                "${args[3]:-}" == "--config" &&
                "${args[4]:-}" == "${CONFIG_FILE}" &&
                "${args[5]:-}" == "run" ]]
            ;;
        *)
            return 1
            ;;
    esac
}

managed_process_running() {
    local pid_file=$1
    local process_type=$2
    local pid
    pid="$(read_pid "${pid_file}")" || return 1
    pid_alive "${pid}" && process_matches "${pid}" "${process_type}"
}

user_systemd_available() {
    command -v systemctl >/dev/null 2>&1 &&
        command -v systemd-run >/dev/null 2>&1 &&
        systemctl --user show-environment >/dev/null 2>&1
}

launch_managed_process() {
    local unit_name=$1
    local working_directory=$2
    local log_file=$3
    shift 3
    LAUNCHED_PID=""

    if user_systemd_available; then
        systemctl --user stop "${unit_name}.service" >/dev/null 2>&1 || true
        systemctl --user reset-failed "${unit_name}.service" >/dev/null 2>&1 || true
        if systemd-run \
            --user \
            --quiet \
            --collect \
            --unit="${unit_name}" \
            --working-directory="${working_directory}" \
            --property="StandardOutput=append:${log_file}" \
            --property="StandardError=append:${log_file}" \
            "${SYSTEMD_ENV_ARGS[@]}" \
            -- "$@"; then
            local attempt service_pid
            for ((attempt = 1; attempt <= 50; attempt++)); do
                service_pid="$(
                    systemctl --user show "${unit_name}.service" \
                        --property=MainPID --value 2>/dev/null || true
                )"
                if [[ "${service_pid}" =~ ^[1-9][0-9]*$ ]] &&
                    pid_alive "${service_pid}"; then
                    LAUNCHED_PID="${service_pid}"
                    return 0
                fi
                sleep 0.1
            done
            warn "用户级 systemd 未返回 ${unit_name} 的有效 PID。"
            systemctl --user stop "${unit_name}.service" >/dev/null 2>&1 || true
            return 1
        fi
        warn "无法创建用户级 systemd 单元 ${unit_name}，回退到普通后台进程。"
    fi

    (
        cd -- "${working_directory}"
        exec nohup "$@"
    ) >>"${log_file}" 2>&1 </dev/null &
    LAUNCHED_PID=$!
}

remove_stale_pid() {
    local pid_file=$1
    local process_type=$2
    local service_name=$3
    [[ -f "${pid_file}" ]] || return 0

    local pid=""
    pid="$(read_pid "${pid_file}" 2>/dev/null || true)"
    if [[ -n "${pid}" ]] && pid_alive "${pid}" &&
        process_matches "${pid}" "${process_type}"; then
        return 0
    fi
    if [[ -n "${pid}" ]] && pid_alive "${pid}"; then
        warn "${service_name} PID 文件指向其他进程 ${pid}；仅移除陈旧 PID 文件，不终止该进程。"
    fi
    rm -f -- "${pid_file}"
}

stop_managed_process() {
    local service_name=$1
    local pid_file=$2
    local process_type=$3
    local pid=""

    pid="$(read_pid "${pid_file}" 2>/dev/null || true)"
    if [[ -z "${pid}" ]]; then
        rm -f -- "${pid_file}"
        log "${service_name} 未运行。"
        return 0
    fi
    if ! pid_alive "${pid}"; then
        rm -f -- "${pid_file}"
        log "${service_name} 已停止，已清理陈旧 PID。"
        return 0
    fi
    if ! process_matches "${pid}" "${process_type}"; then
        warn "${service_name} PID ${pid} 已被其他进程复用；不会终止该进程。"
        rm -f -- "${pid_file}"
        return 0
    fi

    log "正在停止 ${service_name}（PID ${pid}）..."
    kill -TERM "${pid}"
    local attempt
    for ((attempt = 1; attempt <= 30; attempt++)); do
        if ! pid_alive "${pid}"; then
            rm -f -- "${pid_file}"
            log "${service_name} 已停止。"
            return 0
        fi
        sleep 1
    done

    warn "${service_name} 未在 30 秒内退出，将强制终止。"
    kill -KILL "${pid}" >/dev/null 2>&1 || true
    rm -f -- "${pid_file}"
}

port_is_open() {
    local port=$1
    python3 -c \
        'import socket,sys; s=socket.socket(); s.settimeout(0.5); r=s.connect_ex(("127.0.0.1", int(sys.argv[1]))); s.close(); raise SystemExit(0 if r == 0 else 1)' \
        "${port}" >/dev/null 2>&1
}

http_is_ready() {
    local url=$1
    curl --fail --silent --show-error --connect-timeout 2 \
        --max-time 3 \
        --output /dev/null "${url}" 2>/dev/null
}

wait_for_http() {
    local name=$1
    local url=$2
    local pid=$3
    local timeout_seconds=$4
    [[ "${timeout_seconds}" =~ ^[1-9][0-9]*$ ]] ||
        die "${name} 启动超时必须是正整数秒。"
    local attempt
    for ((attempt = 1; attempt <= timeout_seconds; attempt++)); do
        if http_is_ready "${url}"; then
            return 0
        fi
        pid_alive "${pid}" || return 1
        sleep 1
    done
    warn "${name} 未在 ${timeout_seconds} 秒内就绪。"
    return 1
}

simulator_status_healthy() {
    local pid=$1
    local max_age_seconds="${TB_SIMULATOR_STATUS_MAX_AGE:-30}"
    [[ "${max_age_seconds}" =~ ^[1-9][0-9]*$ ]] ||
        die "TB_SIMULATOR_STATUS_MAX_AGE 必须是正整数秒。"
    [[ -x "${PYTHON_BIN}" && -s "${RUNTIME_DIR}/status.json" ]] || return 1
    "${PYTHON_BIN}" -c \
        'import json,sys,time
with open(sys.argv[1], encoding="utf-8") as stream:
    status = json.load(stream)
age_ms = time.time() * 1000 - int(status.get("updated_at", 0))
healthy = int(status.get("pid", 0)) == int(sys.argv[2]) and 0 <= age_ms <= int(sys.argv[3]) * 1000
raise SystemExit(0 if healthy else 1)' \
        "${RUNTIME_DIR}/status.json" "${pid}" "${max_age_seconds}" \
        >/dev/null 2>&1
}

db_container_id() {
    compose ps -q postgres 2>/dev/null || true
}

db_health() {
    local container_id
    container_id="$(db_container_id)"
    [[ -n "${container_id}" ]] || {
        printf 'stopped\n'
        return
    }
    docker_cmd inspect --format \
        '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' \
        "${container_id}" 2>/dev/null || printf 'unknown\n'
}

db_query_scalar() {
    local query=$1
    local result
    result="$(
        compose exec -T \
            -e "PGPASSWORD=${TB_POSTGRES_PASSWORD}" \
            postgres \
            psql \
            --host 127.0.0.1 \
            --port 5432 \
            --username "${TB_POSTGRES_USER}" \
            --dbname "${TB_POSTGRES_DB}" \
            --tuples-only \
            --no-align \
            --set ON_ERROR_STOP=1 \
            --command "${query}" \
            2>/dev/null | tr -d '[:space:]'
    )" || return 1
    printf '%s\n' "${result}"
}

db_core_schema_state() {
    db_query_scalar \
        "SELECT CASE
            WHEN to_regclass('public.tb_schema_settings') IS NOT NULL
                AND to_regclass('public.admin_settings') IS NOT NULL
                THEN 'complete'
            WHEN to_regclass('public.tb_schema_settings') IS NOT NULL
                OR to_regclass('public.admin_settings') IS NOT NULL
                THEN 'partial'
            ELSE 'absent'
        END;"
}

db_install_state() {
    local core_state marker_table_present marker_present
    core_state="$(db_core_schema_state)" || return 1
    marker_table_present="$(db_query_scalar \
        "SELECT to_regclass('public.local_dev_install_state') IS NOT NULL;")" ||
        return 1

    if [[ "${core_state}" == "absent" && "${marker_table_present}" == "f" ]]; then
        printf 'fresh\n'
        return 0
    fi
    if [[ "${core_state}" != "complete" || "${marker_table_present}" != "t" ]]; then
        printf 'partial\n'
        return 0
    fi

    marker_present="$(db_query_scalar \
        "SELECT EXISTS (
            SELECT 1
            FROM public.local_dev_install_state
            WHERE install_key = 'thingsboard-install-complete'
        );")" || return 1
    if [[ "${marker_present}" == "t" ]]; then
        printf 'ready\n'
    else
        printf 'partial\n'
    fi
}

mark_db_install_complete() {
    compose exec -T \
        -e "PGPASSWORD=${TB_POSTGRES_PASSWORD}" \
        postgres \
        psql \
        --host 127.0.0.1 \
        --port 5432 \
        --username "${TB_POSTGRES_USER}" \
        --dbname "${TB_POSTGRES_DB}" \
        --set ON_ERROR_STOP=1 \
        --single-transaction \
        --command \
        "CREATE TABLE IF NOT EXISTS public.local_dev_install_state (
            install_key varchar(64) PRIMARY KEY,
            completed_at timestamptz NOT NULL DEFAULT now()
        );
        INSERT INTO public.local_dev_install_state (install_key)
        VALUES ('thingsboard-install-complete')
        ON CONFLICT (install_key) DO UPDATE
        SET completed_at = EXCLUDED.completed_at;" \
        >/dev/null
}

require_simulator_cli() {
    [[ -x "${PYTHON_BIN}" ]] ||
        die "Python 虚拟环境不存在；请先运行 ./dev.sh prepare。"
    [[ -f "${SCRIPT_DIR}/factory_simulator/__main__.py" ]] ||
        die "模拟器 CLI 不完整：缺少 factory_simulator/__main__.py。"
}

sim_cli() {
    load_env
    require_simulator_cli
    (
        cd -- "${SCRIPT_DIR}"
        "${PYTHON_BIN}" -m factory_simulator --config "${CONFIG_FILE}" "$@"
    )
}

run_prepare() {
    ensure_runtime_layout
    require_command python3 "请安装 Python 3.10 或更高版本。"
    require_command curl "请安装 curl。"
    validate_java
    validate_maven
    require_docker_compose

    if [[ ! -f "${ENV_FILE}" ]]; then
        cp -- "${ENV_EXAMPLE}" "${ENV_FILE}"
        chmod 600 "${ENV_FILE}"
        log "已从 .env.example 创建本地配置 ${ENV_FILE}。"
    else
        chmod 600 "${ENV_FILE}"
        log "保留现有本地配置 ${ENV_FILE}。"
    fi
    load_env
    compose config --quiet

    if [[ ! -x "${PYTHON_BIN}" ]]; then
        if [[ -e "${VENV_DIR}" ]]; then
            die "${VENV_DIR} 已存在但不完整；请手动移走后重新运行 prepare。"
        fi
        log "正在创建 Python 虚拟环境..."
        if ! python3 -m venv "${VENV_DIR}"; then
            die "无法创建虚拟环境；Ubuntu/Debian 请先安装 python3-venv。"
        fi
    fi

    log "正在同步模拟器 Python 依赖..."
    "${PYTHON_BIN}" -m pip install \
        --disable-pip-version-check \
        --requirement "${REQUIREMENTS_FILE}"
    sim_cli validate
    run_log_rotation_install
    log "开发环境准备完成。"
}

run_db_up() {
    load_env
    require_docker_compose
    ensure_runtime_layout
    log "正在启动 PostgreSQL..."
    compose up --detach postgres

    local timeout_seconds="${TB_DB_START_TIMEOUT:-120}"
    [[ "${timeout_seconds}" =~ ^[1-9][0-9]*$ ]] ||
        die "TB_DB_START_TIMEOUT 必须是正整数秒。"
    local attempt health
    for ((attempt = 1; attempt <= timeout_seconds; attempt++)); do
        health="$(db_health)"
        if [[ "${health}" == "healthy" ]]; then
            log "PostgreSQL 已就绪（127.0.0.1:${TB_POSTGRES_PORT}）。"
            return 0
        fi
        if [[ "${health}" == "exited" ]] || [[ "${health}" == "dead" ]]; then
            break
        fi
        sleep 1
    done
    compose logs --no-color --tail 100 postgres >&2 || true
    die "PostgreSQL 未能在 ${timeout_seconds} 秒内就绪。"
}

run_build() {
    validate_java
    validate_maven
    [[ -f "${SCRIPT_DIR}/maven-settings.xml" ]] ||
        die "缺少 ${SCRIPT_DIR}/maven-settings.xml。"
    if managed_process_running "${BACKEND_PID_FILE}" backend ||
        managed_process_running "${UI_PID_FILE}" ui; then
        die "构建会清理运行产物；请先执行 ./dev.sh down。"
    fi

    log "正在构建 ThingsBoard 源码..."
    (
        cd -- "${REPO_ROOT}"
        export MAVEN_OPTS="${MAVEN_OPTS:--Xmx2g}"
        export NODE_OPTIONS="${NODE_OPTIONS:---max_old_space_size=4096}"
        mvn -s "${SCRIPT_DIR}/maven-settings.xml" \
            -T 1C \
            -pl application \
            -am \
            -Ppackaging \
            clean install \
            -DskipTests \
            -Dpkg.skip.deb=true \
            -Dpkg.skip.rpm=true \
            -Dpkg.skip.zip=true
    )
    find_boot_jar >/dev/null
    [[ -x "${UI_DIR}/target/node/node" ]] ||
        die "构建结束但未找到 ui-ngx/target/node/node。"
    [[ -f "${UI_DIR}/node_modules/@angular/cli/bin/ng.js" ||
        -f "${UI_DIR}/node_modules/@angular/cli/bin/ng" ]] ||
        die "构建结束但未找到 Angular CLI。"
    log "源码构建完成。"
}

run_db_init() {
    load_env
    configure_datasource
    validate_java
    run_db_up
    local db_state
    if ! db_state="$(db_install_state)"; then
        die "无法读取数据库初始化状态；请检查 PostgreSQL 日志和连接配置。"
    fi
    case "${db_state}" in
        ready)
            log "数据库已初始化，跳过重复安装。"
            return 0
            ;;
        partial)
            die "数据库中存在不完整或未标记的 ThingsBoard Schema；请检查数据，或执行 ./dev.sh reset --yes 后重建。"
            ;;
        fresh)
            ;;
        *)
            die "无法识别数据库初始化状态：${db_state}"
            ;;
    esac

    local boot_jar
    boot_jar="$(find_boot_jar)"
    local install_java_opts=()
    read -r -a install_java_opts <<<"${TB_INSTALL_JAVA_OPTS:--Xms256m -Xmx2g}"

    log "正在初始化 ThingsBoard 数据库并加载演示数据..."
    LOADER_PATH="${APP_TARGET}/extensions" \
        SQL_DATA_FOLDER="${SQL_DATA_FOLDER:-/tmp}" \
        java "${install_java_opts[@]}" \
        -cp "${boot_jar}" \
        -Dloader.main=org.thingsboard.server.ThingsboardInstallApplication \
        -Dinstall.data_dir="${APP_TARGET}/data" \
        -Dinstall.load_demo=true \
        -Dspring.jpa.hibernate.ddl-auto=none \
        -Dinstall.upgrade=false \
        org.springframework.boot.loader.launch.PropertiesLauncher

    local core_state
    if ! core_state="$(db_core_schema_state)"; then
        die "数据库安装进程已结束，但无法校验 ThingsBoard Schema。"
    fi
    [[ "${core_state}" == "complete" ]] ||
        die "数据库安装进程已结束，但 ThingsBoard 核心 Schema 不完整。"
    mark_db_install_complete
    if ! db_state="$(db_install_state)"; then
        die "数据库安装完成，但无法校验本地完成标记。"
    fi
    [[ "${db_state}" == "ready" ]] ||
        die "数据库安装完成，但写入本地完成标记失败。"
    log "ThingsBoard 数据库初始化完成。"
}

run_backend_start() {
    load_env
    configure_datasource
    validate_java
    require_docker_compose
    ensure_runtime_layout
    local db_state
    if ! db_state="$(db_install_state)"; then
        die "无法读取数据库初始化状态；请确认 PostgreSQL 已启动。"
    fi
    [[ "${db_state}" == "ready" ]] ||
        die "数据库尚未完整初始化；请先运行 ./dev.sh db-init。"

    local existing_health_timeout="${TB_EXISTING_PROCESS_HEALTH_TIMEOUT:-15}"
    [[ "${existing_health_timeout}" =~ ^[1-9][0-9]*$ ]] ||
        die "TB_EXISTING_PROCESS_HEALTH_TIMEOUT 必须是正整数秒。"
    local existing_pid
    if managed_process_running "${BACKEND_PID_FILE}" backend; then
        existing_pid="$(read_pid "${BACKEND_PID_FILE}")"
        if wait_for_http \
            "现有 ThingsBoard 后端" \
            "http://127.0.0.1:${BACKEND_PORT}/" \
            "${existing_pid}" \
            "${existing_health_timeout}"; then
            log "ThingsBoard 后端已运行且健康。"
            return 0
        fi
        warn "ThingsBoard 后端进程存在但服务不健康，将重新启动。"
        stop_managed_process \
            "ThingsBoard 后端" "${BACKEND_PID_FILE}" backend
    fi
    remove_stale_pid "${BACKEND_PID_FILE}" backend "ThingsBoard 后端"
    if port_is_open "${BACKEND_PORT}"; then
        die "端口 ${BACKEND_PORT} 已被非本脚本管理的进程占用。"
    fi

    local boot_jar
    boot_jar="$(find_boot_jar)"
    local backend_java_opts=()
    read -r -a backend_java_opts <<<"${TB_BACKEND_JAVA_OPTS:--Xms512m -Xmx2g}"
    printf '\n===== backend start %s =====\n' "$(date --iso-8601=seconds)" >>"${BACKEND_LOG}"

    log "正在启动 ThingsBoard 后端..."
    export LOADER_PATH="${APP_TARGET}/extensions"
    SYSTEMD_ENV_ARGS=()
    local backend_env_name
    for backend_env_name in \
        LOADER_PATH \
        SPRING_DATASOURCE_URL \
        SPRING_DATASOURCE_USERNAME \
        SPRING_DATASOURCE_PASSWORD \
        DATABASE_TS_TYPE \
        DATABASE_TS_LATEST_TYPE \
        HTTP_BIND_ADDRESS \
        MQTT_BIND_ADDRESS \
        COAP_ENABLED \
        COAP_SERVER_ENABLED \
        LWM2M_ENABLED \
        LWM2M_ENABLED_BS \
        SNMP_ENABLED \
        EDGES_ENABLED \
        TB_GATEWAY_DASHBOARD_SYNC_ENABLED \
        SQL_TTL_TS_ENABLED \
        SQL_TTL_TS_TS_KEY_VALUE_TTL; do
        SYSTEMD_ENV_ARGS+=(--setenv="${backend_env_name}=${!backend_env_name}")
    done
    if ! launch_managed_process \
        "${BACKEND_SYSTEMD_UNIT}" \
        "${REPO_ROOT}" \
        "${BACKEND_LOG}" \
        java "${backend_java_opts[@]}" -jar "${boot_jar}"; then
        SYSTEMD_ENV_ARGS=()
        die "无法启动 ThingsBoard 后端进程。"
    fi
    SYSTEMD_ENV_ARGS=()
    local backend_pid="${LAUNCHED_PID}"
    printf '%s\n' "${backend_pid}" >"${BACKEND_PID_FILE}"

    if ! wait_for_http \
        "ThingsBoard 后端" \
        "http://127.0.0.1:${BACKEND_PORT}/" \
        "${backend_pid}" \
        "${TB_BACKEND_START_TIMEOUT:-240}"; then
        tail -n 100 "${BACKEND_LOG}" >&2 || true
        stop_managed_process \
            "ThingsBoard 后端" "${BACKEND_PID_FILE}" backend
        die "ThingsBoard 后端启动失败；请查看 ${BACKEND_LOG}。"
    fi
    log "ThingsBoard 后端已就绪：http://127.0.0.1:${BACKEND_PORT}"
}

angular_cli_path() {
    if [[ -f "${UI_DIR}/node_modules/@angular/cli/bin/ng.js" ]]; then
        printf '%s\n' "${UI_DIR}/node_modules/@angular/cli/bin/ng.js"
    elif [[ -f "${UI_DIR}/node_modules/@angular/cli/bin/ng" ]]; then
        printf '%s\n' "${UI_DIR}/node_modules/@angular/cli/bin/ng"
    else
        return 1
    fi
}

run_ui_start() {
    ensure_runtime_layout
    local existing_health_timeout="${TB_EXISTING_PROCESS_HEALTH_TIMEOUT:-15}"
    [[ "${existing_health_timeout}" =~ ^[1-9][0-9]*$ ]] ||
        die "TB_EXISTING_PROCESS_HEALTH_TIMEOUT 必须是正整数秒。"
    local existing_pid
    if managed_process_running "${UI_PID_FILE}" ui; then
        existing_pid="$(read_pid "${UI_PID_FILE}")"
        if wait_for_http \
            "现有 Angular 热更新服务" \
            "http://127.0.0.1:${UI_PORT}/" \
            "${existing_pid}" \
            "${existing_health_timeout}"; then
            log "Angular 热更新服务已运行且健康。"
            return 0
        fi
        warn "Angular 热更新进程存在但服务不健康，将重新启动。"
        stop_managed_process \
            "Angular 热更新服务" "${UI_PID_FILE}" ui
    fi
    remove_stale_pid "${UI_PID_FILE}" ui "Angular 热更新服务"
    if port_is_open "${UI_PORT}"; then
        die "端口 ${UI_PORT} 已被非本脚本管理的进程占用。"
    fi

    local node_bin="${UI_DIR}/target/node/node"
    [[ -x "${node_bin}" ]] ||
        die "未找到 ${node_bin}；请先运行 ./dev.sh build。"
    local ng_cli
    ng_cli="$(angular_cli_path)" ||
        die "未找到 Angular CLI；请先运行 ./dev.sh build。"
    printf '\n===== ui start %s =====\n' "$(date --iso-8601=seconds)" >>"${UI_LOG}"

    if ! port_is_open "${BACKEND_PORT}"; then
        warn "后端 ${BACKEND_PORT} 尚未监听；前端会启动，但 API 代理暂不可用。"
    fi
    log "正在启动 Angular 热更新服务..."
    SYSTEMD_ENV_ARGS=()
    if ! launch_managed_process \
        "${UI_SYSTEMD_UNIT}" \
        "${UI_DIR}" \
        "${UI_LOG}" \
        "${node_bin}" \
        --max_old_space_size=8048 \
        "${ng_cli}" \
        serve \
        --configuration development \
        --host 127.0.0.1; then
        die "无法启动 Angular 热更新进程。"
    fi
    local ui_pid="${LAUNCHED_PID}"
    printf '%s\n' "${ui_pid}" >"${UI_PID_FILE}"

    if ! wait_for_http \
        "Angular 热更新服务" \
        "http://127.0.0.1:${UI_PORT}/" \
        "${ui_pid}" \
        "${TB_UI_START_TIMEOUT:-300}"; then
        tail -n 100 "${UI_LOG}" >&2 || true
        stop_managed_process \
            "Angular 热更新服务" "${UI_PID_FILE}" ui
        die "Angular 热更新服务启动失败；请查看 ${UI_LOG}。"
    fi
    log "Angular 热更新服务已就绪：http://127.0.0.1:${UI_PORT}"
}

require_backend_ready() {
    http_is_ready "http://127.0.0.1:${BACKEND_PORT}/" ||
        die "ThingsBoard 后端尚未就绪；请先运行 ./dev.sh backend-start。"
}

run_provision() {
    require_backend_ready
    sim_cli provision
}

run_dashboard() {
    load_env
    if [[ "${TB_PDM_DASHBOARD_MANAGED_PUBLICATION:-false}" == "true" ]]; then
        die "已启用受管仪表盘发布；请先运行 ./dev.sh dashboard-plan，再使用确认的哈希运行 ./dev.sh dashboard-apply。"
    fi
    require_backend_ready
    [[ -f "${RUNTIME_DIR}/devices.json" ]] ||
        die "设备尚未建档；请先运行 ./dev.sh provision。"
    sim_cli dashboard
}

run_dashboard_plan() {
    require_backend_ready
    sim_cli dashboard-plan "$@"
}

run_dashboard_apply() {
    require_backend_ready
    sim_cli dashboard-apply "$@"
}

run_sim_start() {
    load_env
    require_simulator_cli
    ensure_runtime_layout
    require_backend_ready
    [[ -f "${RUNTIME_DIR}/devices.json" ]] ||
        die "设备凭据不存在；请先运行 ./dev.sh provision。"

    local existing_pid
    if managed_process_running "${SIMULATOR_PID_FILE}" simulator; then
        existing_pid="$(read_pid "${SIMULATOR_PID_FILE}")"
        if simulator_status_healthy "${existing_pid}"; then
            log "工厂设备模拟器已运行且状态正常。"
            return 0
        fi
        warn "工厂设备模拟器进程存在但状态文件已过期，将重新启动。"
        stop_managed_process \
            "工厂设备模拟器" "${SIMULATOR_PID_FILE}" simulator
    fi
    remove_stale_pid \
        "${SIMULATOR_PID_FILE}" simulator "工厂设备模拟器"
    port_is_open "${MQTT_PORT}" ||
        die "本机 ThingsBoard MQTT 端口 ${MQTT_PORT} 尚未监听。"
    local simulator_start_timeout="${TB_SIMULATOR_START_TIMEOUT:-120}"
    [[ "${simulator_start_timeout}" =~ ^[1-9][0-9]*$ ]] ||
        die "TB_SIMULATOR_START_TIMEOUT 必须是正整数秒。"
    rm -f -- "${RUNTIME_DIR}/status.json"
    printf '\n===== simulator start %s =====\n' "$(date --iso-8601=seconds)" >>"${SIMULATOR_LOG}"

    log "正在启动配置中的工厂设备模拟器..."
    SYSTEMD_ENV_ARGS=(--setenv=PYTHONUNBUFFERED=1)
    if ! launch_managed_process \
        "${SIMULATOR_SYSTEMD_UNIT}" \
        "${SCRIPT_DIR}" \
        "${SIMULATOR_LOG}" \
        "${PYTHON_BIN}" -m factory_simulator \
        --config "${CONFIG_FILE}" \
        run; then
        SYSTEMD_ENV_ARGS=()
        die "无法启动工厂设备模拟器进程。"
    fi
    SYSTEMD_ENV_ARGS=()
    local launcher_pid="${LAUNCHED_PID}"
    printf '%s\n' "${launcher_pid}" >"${SIMULATOR_LAUNCHER_PID_FILE}"

    local attempt simulator_pid
    for ((attempt = 1; attempt <= simulator_start_timeout; attempt++)); do
        if managed_process_running "${SIMULATOR_PID_FILE}" simulator; then
            simulator_pid="$(read_pid "${SIMULATOR_PID_FILE}")"
            if simulator_status_healthy "${simulator_pid}"; then
                rm -f -- "${SIMULATOR_LAUNCHER_PID_FILE}"
                log "工厂设备模拟器已启动（PID ${simulator_pid}）。"
                return 0
            fi
        fi
        if ! pid_alive "${launcher_pid}"; then
            break
        fi
        sleep 1
    done
    tail -n 100 "${SIMULATOR_LOG}" >&2 || true
    if pid_alive "${launcher_pid}" &&
        process_matches "${launcher_pid}" simulator; then
        kill -TERM "${launcher_pid}" >/dev/null 2>&1 || true
    fi
    rm -f -- "${SIMULATOR_LAUNCHER_PID_FILE}"
    die "工厂设备模拟器启动失败；请查看 ${SIMULATOR_LOG}。"
}

stop_application_processes() {
    stop_managed_process \
        "工厂设备模拟器" "${SIMULATOR_PID_FILE}" simulator
    stop_managed_process \
        "工厂设备模拟器启动进程" "${SIMULATOR_LAUNCHER_PID_FILE}" simulator
    stop_managed_process \
        "Angular 热更新服务" "${UI_PID_FILE}" ui
    stop_managed_process \
        "ThingsBoard 后端" "${BACKEND_PID_FILE}" backend
}

run_up() {
    run_prepare
    stop_application_processes
    run_db_up
    run_build
    run_db_init
    run_backend_start
    run_ui_start
    run_provision
    if [[ "${TB_PDM_DASHBOARD_MANAGED_PUBLICATION:-false}" == "true" ]]; then
        log "已跳过受管预测性维护仪表盘发布；请在审查后运行 dashboard-plan/dashboard-apply。"
    else
        run_dashboard
    fi
    run_sim_start
    sim_cli verify
    log "全部服务已启动。前端：http://127.0.0.1:${UI_PORT}"
}

run_down() {
    ensure_runtime_layout
    stop_application_processes
    if select_docker_command &&
        docker_cmd compose version >/dev/null 2>&1; then
        log "正在停止 PostgreSQL（持久卷会保留）..."
        if compose stop postgres >/dev/null; then
            log "PostgreSQL 已停止。"
        else
            warn "停止 PostgreSQL 失败；应用进程已停止，但数据库容器可能仍在运行。请检查 Docker 状态。"
        fi
    else
        warn "Docker daemon 或 Docker Compose 不可用，未处理 PostgreSQL。"
    fi
    log "本地应用进程已停止；数据库卷 ${DB_VOLUME_NAME} 已保留。"
}

print_process_status() {
    local name=$1
    local pid_file=$2
    local process_type=$3
    local pid=""
    pid="$(read_pid "${pid_file}" 2>/dev/null || true)"
    if [[ -n "${pid}" ]] && pid_alive "${pid}" &&
        process_matches "${pid}" "${process_type}"; then
        printf '  %-12s running (PID %s)\n' "${name}" "${pid}"
    elif [[ -f "${pid_file}" ]]; then
        printf '  %-12s stopped (stale PID file)\n' "${name}"
    else
        printf '  %-12s stopped\n' "${name}"
    fi
}

run_status() {
    load_env_if_present
    printf '服务状态：\n'
    if select_docker_command &&
        docker_cmd compose version >/dev/null 2>&1; then
        printf '  %-12s %s\n' "postgres" "$(db_health)"
    else
        printf '  %-12s unavailable\n' "postgres"
    fi
    print_process_status "backend" "${BACKEND_PID_FILE}" backend
    print_process_status "ui" "${UI_PID_FILE}" ui
    print_process_status "simulator" "${SIMULATOR_PID_FILE}" simulator
    printf '\n访问地址：\n'
    printf '  ThingsBoard API/UI: http://127.0.0.1:%s\n' "${BACKEND_PORT}"
    printf '  Angular hot reload: http://127.0.0.1:%s\n' "${UI_PORT}"
    printf '  MQTT:                127.0.0.1:%s\n' "${MQTT_PORT}"

    if [[ -x "${PYTHON_BIN}" &&
        -f "${SCRIPT_DIR}/factory_simulator/__main__.py" &&
        -f "${ENV_FILE}" ]]; then
        printf '\n模拟器状态：\n'
        sim_cli status || true
    fi
}

run_logs() {
    local component="${1:-all}"
    if [[ $# -gt 0 ]]; then
        shift
    fi
    local follow=false
    if [[ "${1:-}" == "-f" ]] || [[ "${1:-}" == "--follow" ]]; then
        follow=true
        shift
    fi
    [[ $# -eq 0 ]] ||
        die "logs 参数无效；用法：./dev.sh logs [backend|ui|sim|db|all] [-f]"

    case "${component}" in
        db)
            require_docker_compose
            if [[ "${follow}" == true ]]; then
                compose logs --no-color --tail 200 --follow postgres
            else
                compose logs --no-color --tail 200 postgres
            fi
            ;;
        backend | ui | sim)
            local log_file
            case "${component}" in
                backend) log_file="${BACKEND_LOG}" ;;
                ui) log_file="${UI_LOG}" ;;
                sim) log_file="${SIMULATOR_LOG}" ;;
            esac
            [[ -f "${log_file}" ]] || die "日志尚不存在：${log_file}"
            if [[ "${follow}" == true ]]; then
                tail -n 200 -F "${log_file}"
            else
                tail -n 200 "${log_file}"
            fi
            ;;
        all)
            local log_files=()
            [[ -f "${BACKEND_LOG}" ]] && log_files+=("${BACKEND_LOG}")
            [[ -f "${UI_LOG}" ]] && log_files+=("${UI_LOG}")
            [[ -f "${SIMULATOR_LOG}" ]] && log_files+=("${SIMULATOR_LOG}")
            [[ "${#log_files[@]}" -gt 0 ]] ||
                die "应用日志尚不存在。数据库日志可用 ./dev.sh logs db 查看。"
            if select_docker_command &&
                docker_cmd compose version >/dev/null 2>&1; then
                compose logs --no-color --tail 50 postgres || true
            fi
            if [[ "${follow}" == true ]]; then
                tail -n 200 -F "${log_files[@]}"
            else
                tail -n 200 "${log_files[@]}"
            fi
            ;;
        *)
            die "未知日志组件 '${component}'。"
            ;;
    esac
}

run_fault() {
    [[ $# -ge 2 ]] ||
        die "用法：./dev.sh fault DEVICE FAULT [--duration SECONDS] [--replace]"
    managed_process_running "${SIMULATOR_PID_FILE}" simulator ||
        die "模拟器尚未运行；请先运行 ./dev.sh sim-start。"
    sim_cli fault "$@"
}

run_clear() {
    [[ $# -eq 1 ]] || die "用法：./dev.sh clear DEVICE"
    managed_process_running "${SIMULATOR_PID_FILE}" simulator ||
        die "模拟器尚未运行；请先运行 ./dev.sh sim-start。"
    sim_cli clear "$1"
}

run_verify() {
    require_backend_ready
    sim_cli verify
}

run_reset() {
    [[ $# -eq 1 && "$1" == "--yes" ]] ||
        die "reset 会永久删除本地 ThingsBoard 数据；确认后使用 ./dev.sh reset --yes"

    ensure_runtime_layout
    stop_application_processes
    require_docker_compose
    log "正在删除 PostgreSQL 容器、网络和持久卷..."
    compose down --volumes --remove-orphans
    if docker_cmd volume inspect "${DB_VOLUME_NAME}" >/dev/null 2>&1; then
        docker_cmd volume rm "${DB_VOLUME_NAME}" >/dev/null
    fi

    [[ "${RUNTIME_DIR}" == "${SCRIPT_DIR}/.runtime" ]] ||
        die "拒绝清理意外的运行目录：${RUNTIME_DIR}"
    rm -rf -- "${RUNTIME_DIR}"
    ensure_runtime_layout
    log "本地数据库和运行状态已重置；.env、.venv 和源码构建产物未删除。"
}

main() {
    local command="${1:-help}"
    if [[ $# -gt 0 ]]; then
        shift
    fi
    case "${command}" in
        prepare) [[ $# -eq 0 ]] || die "prepare 不接受参数。"; run_prepare ;;
        db-up) [[ $# -eq 0 ]] || die "db-up 不接受参数。"; run_db_up ;;
        build) [[ $# -eq 0 ]] || die "build 不接受参数。"; run_build ;;
        db-init) [[ $# -eq 0 ]] || die "db-init 不接受参数。"; run_db_init ;;
        backend-start) [[ $# -eq 0 ]] || die "backend-start 不接受参数。"; run_backend_start ;;
        ui-start) [[ $# -eq 0 ]] || die "ui-start 不接受参数。"; run_ui_start ;;
        provision) [[ $# -eq 0 ]] || die "provision 不接受参数。"; run_provision ;;
        dashboard) [[ $# -eq 0 ]] || die "dashboard 不接受参数。"; run_dashboard ;;
        dashboard-plan) run_dashboard_plan "$@" ;;
        dashboard-apply) run_dashboard_apply "$@" ;;
        sim-start) [[ $# -eq 0 ]] || die "sim-start 不接受参数。"; run_sim_start ;;
        up) [[ $# -eq 0 ]] || die "up 不接受参数。"; run_up ;;
        down) [[ $# -eq 0 ]] || die "down 不接受参数。"; run_down ;;
        status) [[ $# -eq 0 ]] || die "status 不接受参数。"; run_status ;;
        logs) run_logs "$@" ;;
        log-rotate) [[ $# -eq 0 ]] || die "log-rotate 不接受参数。"; run_log_rotate ;;
        log-rotation-install) [[ $# -eq 0 ]] || die "log-rotation-install 不接受参数。"; run_log_rotation_install ;;
        fault) run_fault "$@" ;;
        clear) run_clear "$@" ;;
        verify) [[ $# -eq 0 ]] || die "verify 不接受参数。"; run_verify ;;
        reset) run_reset "$@" ;;
        help | -h | --help) usage ;;
        *)
            usage >&2
            die "未知命令 '${command}'。"
            ;;
    esac
}

main "$@"
