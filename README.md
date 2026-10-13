# Douyin Presence Monitor

开源、自部署的抖音最近活跃时间监测程序：一个登录账号、多个手动配置的授权目标，命令行管理、独立日志与 Telegram 通知。**没有网页、聊天读取、私信已读检测或默认遥测。**

> 使用抖音网页内部接口，不是承诺稳定的公开 API。只使用平台对登录账号可见的数据。近期活跃不等于确定在线，疑似离线不等于精确退出，不能计算真实连续在线时长，也不保证查询账号隐身。

## 要求与状态语义

- Linux、Python 3.11+；运行依赖只有标准库。原生建议 systemd，也提供非 root Compose。Windows/macOS 不在首版验收范围。
- 只添加自有或同意配合的目标；Telegram Bot 必须有明确接收 chat_id，私聊先 `/start`。
- 默认每 60 秒检测，连接/读取等待 12 秒（不是严格整流程总时限），超过 600 秒才判断疑似离线。
- 连续超时第 1、2 次内部记为未知、记日志、不通知；第 3 次告警一次。短暂抖动静默恢复，已告警未知恢复后才提醒。登录失效/账号不匹配立即提醒。
- 未知不能判下线。重启保留未知计数，已知状态重新建基线，不补造观察中断期间事件。
- 每个目标独立状态和退避；采样与通知分开，Telegram 故障不阻塞采样。共享账号故障按账号报告。
- outbox 固定原始目的地；发送超时后平台可能已经接受，重试可能重复。事件编号帮助识别，不宣称 exactly-once 或用户已读。永久错误保留为 blocked，需明确处理。

## 获取源码

项目仓库：[xxvcc/douyin-presence-monitor](https://github.com/xxvcc/douyin-presence-monitor)。

```sh
git clone https://github.com/xxvcc/douyin-presence-monitor.git
cd douyin-presence-monitor
```

## 构建与测试

```sh
python3 -m venv .venv
.venv/bin/python -m pip install build
.venv/bin/python -m pip install -e .
.venv/bin/python -m build
DPM_TEST_WHEEL="$PWD/dist/douyin_presence_monitor-0.1.0-py3-none-any.whl" \
  PYTHONPATH=src .venv/bin/python -W error::ResourceWarning \
  -m unittest discover -s tests -p 'test_*.py' -v
.venv/bin/douyin-monitor --help
```

测试只用合成数据与回环 HTTP 服务，临时目录遵守 TMPDIR。wheel 是标准 Python 安装包；原生安装器、模板和文档包含在同版本源码发行包中。

本版验收范围与限制见 [发布验证](docs/validation-0.1.0.md)。文件日志按需创建；没有警告/错误时可能尚未产生 error.log。

## 单一安装根目录

默认 `DPM_HOME=/opt/douyin-presence-monitor`，所有**持久化应用文件**都在一个总目录下：

```text
$DPM_HOME/
├── bin/douyin-monitor          # 稳定启动器，按自身位置导出 DPM_HOME
├── releases/0.1.0/venv/        # 版本化运行环境；wheel 也保留在该 release
├── current -> releases/0.1.0   # 仅指向根目录内的版本
├── config/                    # config.json、douyin_session、telegram_token
├── data/                      # monitor.sqlite3、WAL/SHM、monitor.lock、恢复标记
├── logs/                      # app.log、error.log、audit.log 与轮转文件
├── backups/                   # 私有快照与安全回滚副本
├── exports/                   # 私有导出文件
├── systemd/                   # 真实服务单元
└── docs/                      # 运维、安全、变更记录与许可证
```

系统 Python、systemd 是操作系统依赖，不复制到应用目录。原生安装仅在系统中注册固定账号/组 `douyin-monitor` 和服务链接；不在 `/etc`、`/var` 或 `/usr/local/bin` 放另一份应用数据/程序。systemd 自身的临时凭据目录和 journal 属于操作系统设施。

## 原生安装（首次部署）

需要发行版的 `python3-venv`（含 ensurepip），安装根所在文件系统必须允许执行程序（不能是 noexec 挂载）。从同版本源码发行包解压目录执行，提供已核验来源/摘要的 wheel。安装使用 `pip --no-index --no-deps`，**不联网、不初始化配置、不启动或启用服务**。

```sh
export DPM_HOME=/opt/douyin-presence-monitor
WHEEL=/path/to/douyin_presence_monitor-0.1.0-py3-none-any.whl
python3 packaging/native/install.py --wheel "$WHEEL" --root "$DPM_HOME" --systemd --dry-run
sudo python3 packaging/native/install.py --wheel "$WHEEL" --root "$DPM_HOME" --systemd
sudo "$DPM_HOME/bin/douyin-monitor" init
```

安装器只支持**不存在的新根目录**：已有目录（即使空目录）、软链接、路径穿越、不安全的可写父目录、已有同名系统服务均拒绝，没有 `--force`。路径限绝对路径及 ASCII 字母/数字/`_`/`-`/`.`，禁止空格和点路径段。失败保留半成品供人工检查，不自动删除；升级/迁移见运维文档。真实 `--systemd` 根目录只允许位于 `/opt/` 或 `/srv/` 下，祖先必须 root 所有、不可由其他用户写入且允许服务穿越；ProtectHome=yes 不支持在 /root 或 /home 运行。

`--no-systemd` 是默认模式，不创建账号，不执行 systemctl，适合普通用户在自己拥有的目录下离线验证。`--dry-run` 只校验和输出计划，不写文件或执行子进程。账号注册仅在明确 `--systemd` 且以 root 执行时发生。

`init` 要求可信交互终端，隐藏输入秘密、核验身份，在明确同意后发送一条标记为测试的通知。不要把秘密写入参数、聊天、环境变量或普通配置；初始化不覆盖已有配置。root 管理后，在服务停止状态检查/修复所有权（包括 root 创建的锁、日志、WAL/SHM）：

```sh
sudo chown root:douyin-monitor "$DPM_HOME/config" "$DPM_HOME/config/config.json"
sudo chmod 0750 "$DPM_HOME/config"
sudo chmod 0640 "$DPM_HOME/config/config.json"
sudo chown root:root "$DPM_HOME/config/douyin_session" "$DPM_HOME/config/telegram_token"
sudo chmod 0600 "$DPM_HOME/config/douyin_session" "$DPM_HOME/config/telegram_token"
sudo chown -R douyin-monitor:douyin-monitor "$DPM_HOME/data" "$DPM_HOME/logs"
sudo chown douyin-monitor:douyin-monitor "$DPM_HOME/backups" "$DPM_HOME/exports"
sudo chmod 0700 "$DPM_HOME/data" "$DPM_HOME/logs" "$DPM_HOME/backups" "$DPM_HOME/exports"
sudo "$DPM_HOME/bin/douyin-monitor" config validate
sudo systemctl enable --now douyin-presence-monitor.service
sudo "$DPM_HOME/bin/douyin-monitor" status
sudo "$DPM_HOME/bin/douyin-monitor" doctor
```

仅对自己控制、已检查无异常链接的实例执行权限修复。不要递归 chown backups，其中单独保管的秘密回滚副本必须保持 root-only。代码、启动器、`current` 与单元由 root 管理，服务只写 data/logs/backups。普通配置 root:服务组 `0640`、目录 `0750`；**秘密始终 root-only `0600`**，只通过 `LoadCredential` 注入。服务读取真实 config.json，取得实例锁后重新读取；不把配置做成凭据快照。真实单元在根目录内，`systemctl link` 只注册链接。

可手动 `export PATH="$DPM_HOME/bin:$PATH"`，不必创建全局可执行链接。稳定启动器以自身位置为准，不让传入的 DPM_HOME 重定向到别的实例。直接使用 Python 模块/venv 控制台脚本时则由 DPM_HOME 选择默认根目录。

## Docker Compose（首次部署）

容器内代码与持久化挂载都在 `/app` 下，UID/GID 为 `10001:10001`；系统 Python 是镜像依赖。宿主机所有实例持久化内容集中在一个 `instance/` 根目录；**镜像层和 Docker 元数据由 Docker 单独管理，不位于 instance 数据目录**。

```sh
cd packaging/docker
export DPM_INSTANCE_ROOT="$PWD/instance"
docker compose build
sudo install -d -m 0700 -o 10001 -g 10001 "$DPM_INSTANCE_ROOT" \
  "$DPM_INSTANCE_ROOT/config" "$DPM_INSTANCE_ROOT/data" \
  "$DPM_INSTANCE_ROOT/logs" "$DPM_INSTANCE_ROOT/backups" "$DPM_INSTANCE_ROOT/exports"
docker compose -f compose.yaml -f compose.setup.yaml run --rm -it monitor init
docker compose up -d
docker compose exec monitor /app/current/venv/bin/douyin-monitor status
docker compose exec monitor /app/current/venv/bin/douyin-monitor doctor
```

日常配置只读、根文件系统只读、不发布端口、不挂载 Docker socket；bind 目录必须预建，Compose 不自动创建 root 所有目录。Docker 秘密由容器 UID 持有 `0600`，不能沿用原生 root-only 源文件权限。`/app/tmp` 是有限额私有 tmpfs。配置变更先停服务，仅一次性管理命令使用 setup override（只放开 config 挂载，根文件系统仍只读）：

```sh
docker compose stop monitor
docker compose -f compose.yaml -f compose.setup.yaml run --rm -it monitor notify setup
docker compose up -d
```

恢复同样需要 setup override。不要用 override 启动长期服务。容器 `127.0.0.1` 不是宿主机代理；默认 `telegram_proxy: null`，确需代理时配置容器可达地址，不关闭防火墙。

## 日常命令

以下假设已把根目录的 bin 加入 PATH，原生私有数据管理通常由 root 执行：

```sh
douyin-monitor status
douyin-monitor --json status
douyin-monitor doctor
douyin-monitor doctor --network
douyin-monitor config validate
douyin-monitor config show
douyin-monitor targets list
douyin-monitor logs --follow
douyin-monitor logs --level warning
douyin-monitor logs --stream audit
douyin-monitor events --since today
douyin-monitor notify test
douyin-monitor backup
douyin-monitor export --format csv --output "$DPM_HOME/exports/events.csv"
```

`status`、普通 `doctor`、日志/事件查询不访问账号接口，不创建空数据库。`doctor --network`、`probe`、通知测试是显式网络操作。全局路径与 `--json` 参数在子命令之前。

`DPM_HOME` 派生 config/data/logs/backups 默认路径。导出默认应放在根内 exports，不允许覆盖配置、秘密、数据库等控制路径。迁移/测试可显式覆盖 `--config`、`--credentials`、`--state-dir`、`--log-dir`，对应 `DPM_CONFIG`、`DPM_CREDENTIALS`、`DPM_STATE_DIR`、`DPM_LOG_DIR`。备份默认位于根目录的 backups；显式输出到根外是操作者的另行选择。配置/目标/机器人切换要求停服务；重启后使用新配置，不承诺热更新。

## 通知、日志和隐私

- `thread_id: null` 为普通私聊，不沿用其他机器人的话题 ID。用 `notify setup` 切换而非只改 Token，必须核验身份、目的地和未解决队列。
- blocked 可显式重试或附理由放弃；操作保留审计，不偷偷改发历史通知。
- error.log 含 WARNING 及以上；日志按天/大小轮转并受保留期/总容量限制，文件写入失败退回 stderr/journald。
- 普通备份不含 Cookie/Bot Token，但有目标、接收位置和活动历史，仍是私人数据。凭据是权限保护的明文，不宣称磁盘加密。
- 主机或进程完全停止时无法靠自己发离线通知，需要独立外部健康监测。

配置示例：`examples/config.example.json`；恢复、升级和回滚：[运维文档](docs/operations.md)；安全边界：[SECURITY.md](SECURITY.md)。

## 开源与验收状态

当前版本为 **0.1.0**，采用 MIT 许可，运行依赖只有 Python 标准库。配置与数据库 schema 独立编号，当前均为 2。

测试覆盖范围与复现步骤见 [发布验证](docs/validation-0.1.0.md)，远端结果以 [GitHub Actions](https://github.com/xxvcc/douyin-presence-monitor/actions) 为准。安装包与校验值从 [Releases](https://github.com/xxvcc/douyin-presence-monitor/releases/tag/v0.1.0) 获取；Docker 镜像可自行构建，尚未上传公共镜像仓库。

仓库和发行包仅包含源码、文档、测试及合成示例，不应提交真实凭据、配置、数据库、目标标识或备份。
