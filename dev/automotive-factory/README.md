# 汽车零部件工厂本地开发环境

该目录提供面向 ThingsBoard 二次开发的一键本地环境：

- ThingsBoard 后端：本机 JDK 进程，便于断点调试和重启。
- ThingsBoard UI：本机 Angular 开发服务器，支持热更新。
- MQTT 传输：由本机 ThingsBoard 直接提供，端口 `1883`，不部署 Mosquitto。
- PostgreSQL 16：Docker Compose 容器，数据保存在独立命名卷中。
- 工厂模拟器：项目内 Python 虚拟环境，模拟两条产线的 20 台设备。

所有 PID、日志、设备 Token 和模拟器状态都写入 `.runtime/`；租户和数据库密码写入本地 `.env`。这两个位置都已被 Git 忽略。
本地历史遥测默认保留 7 天；应用日志默认在 10 MB 时轮转并保留 2 份压缩文件，PostgreSQL 容器日志也设有大小上限。
HTTP、MQTT、Angular 和 PostgreSQL 默认均只绑定 `127.0.0.1`；当前阶段未使用的 CoAP、LwM2M、SNMP 和 Edge RPC 默认关闭，网关仪表板的外部同步也默认关闭。
如果当前用户的 systemd 可用，三个本机开发进程会由临时用户单元托管，退出终端后仍持续运行；否则脚本自动回退到普通后台进程。临时单元不会设置开机自启。

## 前置条件

- JDK 25，并确保 `java -version` 与 `mvn -version` 都显示 Java 25。
- Maven 3.6.3 或更高版本。
- Docker Engine 与 Docker Compose v2。
- Python 3.10 或更高版本，并安装 `venv` 支持。
- `curl`。
- 建议至少预留 8 GB 内存和 15 GB 磁盘空间。

Ubuntu/Debian 如果无法创建 Python 虚拟环境，可安装：

```bash
sudo apt install python3-venv
```

## 一键启动

从仓库根目录执行：

```bash
cd dev/automotive-factory
./dev.sh up
```

`up` 会依次：

1. 创建 `.env` 和 Python 虚拟环境并安装依赖。
2. 启动 PostgreSQL。
3. 编译当前 ThingsBoard 源码。
4. 在空数据库中安装表结构和演示数据。
5. 启动后端和 Angular 热更新服务。
6. 自动创建或复用 20 台设备并保存独立 Access Token。
7. 创建或更新基础工厂仪表盘。
8. 启动 MQTT 模拟器并执行验收检查。

首次构建需要下载 Maven、Node、Yarn 和 Python 依赖，耗时取决于网络情况。脚本不会使用仓库上游 `install_dev_db.sh`，数据库初始化直接以当前用户运行，不依赖 `sudo`。

启动完成后访问：

- Angular 热更新页面：<http://127.0.0.1:4200>
- 后端页面/API：<http://127.0.0.1:8080>
- MQTT：`127.0.0.1:1883`
- PostgreSQL：`127.0.0.1:5432`，或 `.env` 中配置的端口

默认租户管理员账号来自 `.env`：

```text
tenant@thingsboard.org / tenant
```

## 运维命令

| 命令 | 作用 |
| --- | --- |
| `./dev.sh prepare` | 创建本地配置、虚拟环境并安装模拟器依赖 |
| `./dev.sh db-up` | 启动 PostgreSQL 并等待健康检查通过 |
| `./dev.sh build` | 编译当前 ThingsBoard 源码及 UI |
| `./dev.sh db-init` | 初始化数据库；仅在完整 Schema 和本地完成标记都存在时跳过 |
| `./dev.sh backend-start` | 后台启动本机 ThingsBoard，日志写入 `.runtime` |
| `./dev.sh ui-start` | 后台启动 Angular 开发服务器和热更新 |
| `./dev.sh provision` | 创建或复用 20 台设备并刷新本地 Token 文件 |
| `./dev.sh dashboard` | 仅非受管本地模式：创建或更新基础工厂仪表盘 |
| `./dev.sh dashboard-plan --actor ACTOR --output /absolute/path/plan.json` | 只读生成预测性维护仪表盘的 30 分钟受管发布计划，并将 canonical JSON 写入指定绝对路径 |
| `./dev.sh dashboard-apply --plan /absolute/path/plan.json --plan-hash SHA256 --confirmed-hash SHA256 --actor ACTOR --receipt /absolute/path/receipt.json` | 仅用同一计划哈希的后续明确确认发布一次仪表盘，并将回执写入指定绝对路径 |
| `./dev.sh sim-start` | 启动 20 个使用独立 Token 的 MQTT 客户端 |
| `./dev.sh up` | 执行完整构建和启动流程 |
| `./dev.sh down` | 停止模拟器、UI、后端和数据库，保留数据库卷 |
| `./dev.sh status` | 查看数据库、进程和模拟器状态 |
| `./dev.sh logs [组件] [-f]` | 查看日志；组件为 `backend`、`ui`、`sim`、`db` 或 `all` |
| `./dev.sh log-rotate` | 立即检查并轮转达到大小上限的应用日志 |
| `./dev.sh log-rotation-install` | 安装并启动每分钟执行的用户级日志轮转定时器 |
| `./dev.sh fault ...` | 向运行中的模拟器注入故障 |
| `./dev.sh clear DEVICE` | 清除指定设备的活动故障 |
| `./dev.sh verify` | 通过 REST 验证 20 台设备及其最新遥测 |
| `./dev.sh reset --yes` | 永久删除本地数据库卷和 `.runtime` 状态 |

单独重新编译前，应先执行 `./dev.sh down`。`build` 使用 Maven `clean`，脚本会拒绝在受管理的后端或 UI 仍运行时清理构建产物。

## 模拟设备

`config.yml` 默认配置两条产线，每条 10 台：

| 类型 | 每条产线 | 总数 |
| --- | ---: | ---: |
| CNC 加工中心 | 2 | 4 |
| 注塑机 | 2 | 4 |
| 装配机器人 | 2 | 4 |
| 自动拧紧设备 | 2 | 4 |
| 空压机 | 1 | 2 |
| EOL 检测台 | 1 | 2 |

设备名类似 `LINE-A-CNC-01`。每台设备使用独立 Access Token，以 MQTT QoS 1 每 2 秒向 `v1/devices/me/telemetry` 上报 JSON。设备数量、周期、指标范围、自动故障概率和故障覆盖值均在 `config.yml` 中配置。为保证首次验收可重复，自动随机故障默认关闭；将 `simulation.automatic_faults.enabled` 改为 `true` 即可启用。

修改 `config.yml` 后重新执行完整启动流程。脚本会重新校验配置、幂等建档并重启模拟器：

```bash
./dev.sh down
./dev.sh up
```

只有拓扑发生变化时才会创建新设备；名称相同的现有设备及其 Token 会被复用。

## 手动故障注入

查看可用设备与当前状态：

```bash
./dev.sh status
```

注入持续 60 秒的高温故障：

```bash
./dev.sh fault LINE-A-CNC-01 HIGH_TEMPERATURE --duration 60
```

注入非计划停机，保持到手动清除：

```bash
./dev.sh fault LINE-B-EOL_TESTER-01 UNPLANNED_STOP
```

替换设备当前已有的故障：

```bash
./dev.sh fault LINE-A-CNC-01 TOOL_WEAR --replace
```

手动清除：

```bash
./dev.sh clear LINE-A-CNC-01
```

可配置故障包括：

- `HIGH_TEMPERATURE`
- `HIGH_VIBRATION`
- `OVERLOAD`
- `UNPLANNED_STOP`
- `QUALITY_FAILURE`
- `TOOL_WEAR`
- `ROBOT_COLLISION`
- `PRESSURE_ANOMALY`
- `TIGHTENING_NOK`
- `CALIBRATION_DUE`
- `COMMUNICATION_LOSS`

注入和恢复会立即上报状态事件，并通过 ThingsBoard REST API 创建或清除对应 Alarm。周期遥测仍按配置继续发送；通信中断模式会临时断开该设备的 MQTT 连接。

## 日志与排障

查看最近日志：

```bash
./dev.sh logs backend
./dev.sh logs ui
./dev.sh logs sim
./dev.sh logs db
./dev.sh logs all
```

持续跟踪：

```bash
./dev.sh logs backend -f
```

日志文件位置：

```text
.runtime/logs/backend.log
.runtime/logs/ui.log
.runtime/logs/simulator.log
```

日志上限由 `.env` 中的 `TB_LOG_ROTATE_SIZE` 和 `TB_LOG_ROTATE_COUNT` 控制。`prepare` 会安装用户级 systemd 定时器，每分钟检查一次；如果用户级 systemd 不可用，可手动运行 `./dev.sh log-rotate`。

常见问题：

- `8080`、`1883` 或 `4200` 被占用：运行 `./dev.sh status`，再检查机器上的其他服务。脚本不会终止不受它管理的进程。
- Angular 报 `System limit for number of file watchers reached`：安装仓库提供的配置后重新启动前端：`sudo install -m 0644 99-thingsboard-dev-inotify.conf /etc/sysctl.d/99-thingsboard-dev-inotify.conf && sudo sysctl --system`。
- Maven 显示 Java 不是 25：修正 `JAVA_HOME` 和 `PATH` 后重新执行。
- 缺少 `ui-ngx/target/node/node`：先运行 `./dev.sh build`。
- 设备 Token 不存在：运行 `./dev.sh provision`。
- 后端启动失败：优先查看 `./dev.sh logs backend`。
- 模拟器发布失败：确认后端 MQTT 端口 `1883` 已监听，并查看 `./dev.sh logs sim`。
- 用户级 systemd 可用时，可用 `systemctl --user status tb-automotive-backend tb-automotive-ui tb-automotive-simulator` 查看进程；正常启停仍统一使用 `dev.sh`。

## 数据持久化与重置

`down` 只停止服务，不删除以下内容：

- PostgreSQL 命名卷 `tb-automotive-postgres-data`
- `.runtime/devices.json` 中的设备 Token
- 本地日志和状态
- `.env`
- `.venv`

因此再次启动会复用原数据库和设备。

历史遥测保留时间由 `.env` 中的 `SQL_TTL_TS_TS_KEY_VALUE_TTL` 控制，单位为秒，默认 `604800`（7 天）；设置为 `0` 表示永不过期。TTL 清理由 ThingsBoard 定时执行，删除旧数据后 PostgreSQL 已分配给表的空间不一定立即归还给操作系统。

只有以下命令会永久删除 PostgreSQL 卷和 `.runtime`：

```bash
./dev.sh reset --yes
```

`reset` 不删除 `.env`、`.venv` 或 Maven 构建产物。重置后重新执行 `./dev.sh up` 即可创建全新环境。

## 预测性维护仪表盘发布

试点仪表盘的设备表会以只读 Server Attribute 显示 `equipment_id` 与
`cmms_asset_id`。模拟器不会生成或写入这两个标识；它们由后续经确认的
设备建档流程提供。告警表不含操作按钮，本阶段也不会变更告警。

默认 `.env.example` 通过 `TB_PDM_DASHBOARD_MANAGED_PUBLICATION=true` 启用受管
发布。先通过只读身份发现确认租户 UUID，再将其复制到本地、未跟踪的 `.env`
中的 `TB_PDM_EXPECTED_TENANT_ID`。之后用必填的 `--actor` 和 `--output`
执行 `dashboard-plan`，审查输出的租户、当前/目标内容哈希及计划哈希，并在另一个
明确确认步骤将同一个 64 位小写 SHA-256 同时传给 `--plan-hash` 和
`--confirmed-hash`。`dashboard-apply` 还必须显式指定原计划的 `--plan`、
执行者 `--actor` 与新回执的 `--receipt`。计划仅有效 30 分钟；应用前会重新检查
租户、仪表盘 ID、版本和内容哈希，且至多保存一次。计划、消费标记和回执均为权限
`0600` 的无凭据 strict canonical JSON；计划超过 8 MiB 或包含非有限数值时会在
创建任何文件前被拒绝，tuple、自定义 iterable 等非 JSON 原生容器同样不被接受。
消费权按已确认的计划 SHA-256 唯一绑定，并固定
记录在 `.runtime/.pdm-dashboard-consumption/` 这个权限为 `0700`、由当前用户拥有
的命名空间中；从配置根目录到该命名空间的每一级都必须由当前用户拥有、不可经由
符号链接且不可由 group/world 写入。复制同一计划到其他路径不会获得新的消费权。
计划和回执目标必须是安全、互不相同且父目录已存在的绝对路径，已存在、符号链接
或不安全父目录会被拒绝。
运行受管命令前，操作员必须通过外部环境提供已认证的
`TB_PDM_DASHBOARD_BEARER_TOKEN`；计划命令绝不执行登录请求，且该凭据不会写入
计划、回执或错误输出。受管模式下旧的 `dashboard` 写命令会拒绝执行。
