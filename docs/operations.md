# 运维、备份、迁移与回滚

以下以原生 root 管理、固定非 root `douyin-monitor` 账号运行举例：

```sh
export DPM_HOME=/opt/douyin-presence-monitor
export PATH="$DPM_HOME/bin:$PATH"
```

所有应用持久文件在该根目录下；配置在 config，SQLite/锁/恢复标记在 data，JSON 日志在 logs，备份在 backups，导出在 exports，版本在 releases，真实单元在 systemd。`/etc/systemd/system/douyin-presence-monitor.service` 仅是指向根内单元的注册链接。只有 OS 账号/组与服务注册在根外；系统依赖、journal 和 systemd 临时凭据不属于应用持久文件。

## 健康与日志

```sh
douyin-monitor status
douyin-monitor doctor
systemctl status douyin-presence-monitor.service --no-pager
journalctl -u douyin-presence-monitor.service -n 100 --no-pager
douyin-monitor logs --stream error --limit 100
```

分别看进程心跳、采集健康、目标活动估计和 outbox。心跳新鲜不代表上游或 Telegram 正常。容器健康探针读 `$DPM_HOME/data` 下心跳，不把上游登录失效当作需要疯狂重启的理由。日志轮转/清理由应用执行，不依赖外部 `/var/log` 文件。

## 修改、秘密与权限

```sh
systemctl stop douyin-presence-monitor.service
douyin-monitor config set poll_seconds 60
douyin-monitor config validate
# 核对下述权限，再启动
systemctl start douyin-presence-monitor.service
```

目标使用 `targets add|pause|resume|remove`（参数见 --help）；移除配置保留历史，不自动发现好友。抖音凭据用 `credentials update` 在可信 TTY 隐藏输入并验证同一观察账号。不同观察账号须新建独立实例，不改 observer_uid 绕过数据库绑定。

Telegram 用 `notify setup`：新机器人先 `/start`，普通私聊不要旧话题；pending/blocked 未解决前拒绝切换。`outbox retry ID` 只重试原目的地，`outbox discard ID --reason TEXT` 是明确放弃并保留审计，不能为了通过检查自动清空。

原生权限契约：

| 路径 | 所有者 | 权限 |
|---|---|---|
| 根、bin、releases、current、systemd、docs | root:root | 目录/启动器 0755，普通代码/文档 0644；服务不可写 |
| config/ | root:douyin-monitor | 0750 |
| config/config.json | root:douyin-monitor | 0640；普通配置非秘密 |
| config/douyin_session、telegram_token | root:root | 0600，绝不能授予服务组读取 |
| data/、logs/、backups/、exports/ | douyin-monitor:douyin-monitor | 0700，运行数据文件 0600 |

单元只对两个秘密使用 LoadCredential，配置使用真实源路径，取得实例锁后重新读取。不要新增 `LoadCredential=config.json`，否则 pause/remove 与启动可能使用不同配置快照。不要用 DynamicUser、StateDirectory、LogsDirectory 把数据重新分散到 `/var`。

root CLI 维护可能创建 root 所有的锁、日志、安全快照或 WAL/SHM。服务停止后，检查无异常链接，按 README 修复 data/logs 与备份目录本身的所有权（不要递归改变 backups 内 root-only 秘密副本）；普通配置保持 0640 与服务组，秘密保持 0600。不得递归放开 config 权限。运行中不要 chown 数据库或删锁文件。

## 备份与恢复

```sh
douyin-monitor backup
```

默认 `$DPM_HOME/backups/` 每次创建独立私有目录，含 config.json、monitor.sqlite3、manifest.json；SQLite backup API 提供在线一致性快照，不直接复制运行中的 SQLite/WAL/SHM。普通备份**不含秘密**；需要另在可信密码管理器或受控 root-only 备份保存凭据。备份含私人目标、目的地和历史，不能公开上传。

恢复前核验摘要、quick_check、支持的版本/账号绑定，并在隔离目录真正恢复、核对计数与队列：

```sh
systemctl stop douyin-presence-monitor.service
douyin-monitor restore "$DPM_HOME/backups/<backup-directory>"
douyin-monitor config validate
# 检查恢复后凭据仍匹配账号/机器人；修复运行目录所有权
systemctl start douyin-presence-monitor.service
douyin-monitor status
```

restore 拒绝运行中的实例，验证后保留替换前安全快照。配置与 SQLite 的普通文件替换不是跨资源原子事务。若 data 有 init/通知切换/restore 的恢复标记，先看对应快照与操作记录，**不要删除标记或修改绑定强行启动**。通知切换中断用停止状态的 `notify recover`；restore 中断依本版本恢复协议重试同一已核验备份，不混用不相关快照。初始化中断须人工确认其部分文件再重新 init。修复后检查配置、数据库、凭据和目录权限完整一致。

## 原生安装器的边界

`packaging/native/install.py` 是离线、首次安装工具，需要同版本源码发行包和 wheel。默认 `--no-systemd`；只有显式 `--systemd` 才创建系统账号、执行 systemctl link/daemon-reload。它不 init、不 enable、不 start。

- 新根目录必须不存在，父目录已存在且受信任；禁止软链接祖先与路径穿越。
- 已有根目录、已有系统单元一律拒绝，包含空目录、半成品、运行实例和同版本 release。没有覆盖/强制选项。
- 安装失败保留现场，不自动删除用户文件或回滚 OS 账号。管理员核查后可只移除本次新建的半成品再重试。
- `--dry-run` 无写入/子进程；它显示请求的系统注册计划，不代表目标机器的实际账号/服务冲突已通过真实安装检查。
- 真实 `--systemd` 根只允许在 `/opt/` 或 `/srv/` 下，祖先必须 root 所有、不可由其他用户写入且可穿越；ProtectHome=yes 下不能在 /root 或 /home 运行。
- `--no-systemd` 允许普通用户在私有目录验证；目录由当前用户持有，不自动将其提升成生产服务。
- 不把已建 venv 随意移动到另一根：pip 控制台脚本有绝对 shebang。搬迁需在最终路径重新建 venv，并重新生成单元路径。

## 同一根内升级

产品发布版本与数据库 schema 是独立编号。0.1.0 使用 schema 2；升级或回滚必须检查配置和数据库兼容性，不能仅比较产品版本号。保留历史备份标签以便追溯，不改写已有备份的版本或数据。

首次安装器不会覆盖旧根。升级由管理员显式操作，发布方需先完成测试。保持稳定 bin 启动器和现有 config/data/logs/backups；不要在原 venv 中 pip upgrade。

1. 在根内 `releases/<新版本>/` 创建全新目录（`mkdir` 而不是忽略已存在的 `mkdir -p`）；拒绝已有版本、任何软链接或异常所有者。
2. 放入核验过的可信 wheel，在**最终路径**创建 venv；例如 `python3 -m venv "$DPM_HOME/releases/<新版本>/venv"`，随后用该 venv Python 执行 `-m pip --isolated install --no-index --no-deps --no-cache-dir <wheel绝对路径>`。先 `--version`、`--help`，不启动第二个实例。
3. 若运行代码无需变动配置，可以在旧服务继续运行时完成该离线 staging；**切换前必须停止服务**并检查没有其他实例持有 data/monitor.lock。保留完整一致性备份、旧 current 目标、旧单元和凭据。
4. 检查新版本数据库/配置兼容性，在隔离副本验证迁移；不以真实目标作离线测试，不自动增删目标。
5. 在根内建立临时相对链接指向 `releases/<新版本>`，确认旧 current 也是根内链接，然后原子替换 current。不要用未校验的任意路径覆盖 current；不要在运行中切换。
6. 如果新版本改变服务沙箱或启动器，先备份旧文件到根内 backups，再按发布模板更新根内单元（替换 `@DPM_HOME@` 为真实根）和 bin。用 `systemd-analyze verify "$DPM_HOME/systemd/douyin-presence-monitor.service"` 检查，再 daemon-reload；注册链接仍指根内同一单元。
7. 修复权限后启动。核验实际 Python/版本、有效 systemd 路径、身份门禁、真实采样、outbox 与独立日志。明确通知测试必须核对准确 chat/thread/text 回执，不能只看 HTTP 200。

## 从旧分散式布局迁移

旧原型可能用 `/etc/douyin-presence-monitor` 存配置、`/var/lib/douyin-presence-monitor` 存数据、`/var/log/douyin-presence-monitor` 存日志，或在旧 `/opt/douyin-presence-monitor` 根内混放程序。上述仅是**旧来源**，不是新默认路径。

1. 在独立测试目录中完成离线 wheel、Docker、v1→v2 数据副本迁移验证，旧服务继续工作。不要让安装器对现有根强制覆盖。
2. 记录旧有效单元、版本、current/程序路径、账号 UID/GID、真实数据库和配置来源。DynamicUser 的 `/var/lib`/`/var/log` 可能是 systemd 私有目录别名，必须找到真实来源，不盲目跟随任意链接。
3. 经明确维护窗口授权后停止旧服务，确认锁与进程退出；制作一致性数据库/配置/秘密备份，保留旧程序和单元。核对样本数、事件 ID、未知计数、发送状态、旧 chat/thread/bot 绑定。
4. 为首次安装腾出**不存在的最终根**：如旧程序已占用默认根，先将它完整保留在受控临时回滚位置，不能直接删除。备份旧系统单元并移除其旧注册（包括旧 enable 链接），daemon-reload；不要影响其他服务。
5. 对最终根执行新安装器 `--systemd`（不启动），随后把旧来源的匹配配置/秘密放入 config、SQLite 放入 data、保留日志放入 logs；在根内 backups 保存完整旧程序/配置/秘密/单元回滚快照（秘密副本 root-only）。**已有配置不运行 init**；旧 v1 配置由 CLI 校验规范化，数据库迁移先用隔离副本验证。
6. 按权限表修复所有者，config validate、离线 doctor；授权启动后核验绑定、采样、旧队列和准确测试回执。不得伪造丢失时间段事件或默默丢弃队列。
7. 仅在验收和回滚保留期满足后清理旧 `/etc`、`/var` 来源与临时回滚位置，避免留下第二套明文秘密。最终应用负载/数据只保留在新根；注册链接与专用 OS 账号除外。

## 回滚

- 先停止当前服务、保存失败现场。程序回滚必须匹配配置/数据库版本；v2 数据库不能直接交给 v1 程序。
- 对兼容数据库的同根升级，可以将 current 原子换回旧根内 release，并恢复匹配单元/启动器。对模式迁移，恢复同一时间点的旧程序、配置、凭据、数据库与单元。
- 检查私有目录、锁、WAL/SHM 与日志所有者；daemon-reload、再启动，核验身份、采样和通知绑定。
- 恢复旧数据会丢失备份之后的记录，已发到 Telegram 的消息不能撤销；不宣称跨平台无损事务回滚。

## Docker 维护、升级与回滚

Compose 的 `DPM_INSTANCE_ROOT` 必须始终指向同一个绝对宿主目录；其 config/data/logs/backups/exports 对应容器 `/app` 子目录。镜像另由 Docker 管理，不属于宿主 instance。

```sh
# 在 packaging/docker，先设置 DPM_INSTANCE_ROOT
docker compose stop monitor
docker compose -f compose.yaml -f compose.setup.yaml run --rm -it monitor config validate
docker compose -f compose.yaml -f compose.setup.yaml run --rm -it monitor restore /app/backups/<backup-directory>
# 核验后，用不含 setup override 的配置启动
docker compose up -d
```

日常只读命令无需 override；init/config/targets/credentials/notify setup/restore 等写配置命令需 override。根文件系统仍只读、同样非 root。新镜像先用唯一测试标签在隔离实例离线验证，再停止旧容器、备份、切换镜像。回滚保留旧镜像 ID/摘要与匹配数据快照，不能只换 tag 而忽略模式版本。Docker 不需要宿主专用系统账号；挂载所有者是 UID/GID 10001，秘密 0600。不要把宿主原生 root-only 秘密改成组可读来凑容器权限。

## 卸载与网络诊断

停止并禁用服务后，仅移除本程序注册链接；根目录及专用账号默认保留供恢复。彻底删除根目录/账号须另行确认，先检查账号没有其他用途。不删除共享 Python、Docker、代理服务。Compose 使用 down 时不顺带删除实例数据。

抖音直连、Telegram 用显式配置代理，不继承 shell HTTP_PROXY；分别诊断。非 JSON、零时间、字段缺失、429、超时都不等于目标离线，不关闭 TLS 校验或绕过身份绑定。

## 验收边界

部署验证必须区分：离线单元测试、wheel/sdist 安装、模板/Compose 语法、真实非 root 沙箱、升级恢复演练、最终获授权账号的实际采样和精确通知回执。前几项成功不能替代最后的部署验收。
