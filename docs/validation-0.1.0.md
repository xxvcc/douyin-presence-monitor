# 0.1.0 验证范围与复现步骤

本页说明公开版本的测试方法与边界，不包含私人部署记录、真实配置或账号数据。程序版本与存储 schema 独立编号；0.1.0 使用配置和数据库 schema 2。

## 自动测试覆盖

- 最近活跃、疑似离线、未知状态及跨重启的连续超时计数。
- 多目标独立状态、共享账号故障合并、通知排序与永久失败处理。
- 命令行校验、隐藏凭据输入、配置变更锁和中断恢复。
- SQLite 迁移、备份完整性、恢复与不可覆盖的安全导出。
- 日志脱敏、轮转、保留策略、并发写入与不同用户间的文件权限。
- 单目录布局、离线 wheel 安装、危险路径和重复安装拒绝。
- 公开文档的路径可移植性，以及包、CLI、Docker 的版本一致性。

测试使用合成数据和回环 HTTP 服务，不需要真实抖音或 Telegram 凭据，也不应指向正在运行的实例。

## 运行完整本地测试

从源码根目录执行：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install build
.venv/bin/python -m pip install -e .
.venv/bin/python -m build
DPM_TEST_WHEEL="$PWD/dist/douyin_presence_monitor-0.1.0-py3-none-any.whl" \
  PYTHONPATH=src .venv/bin/python -W error::ResourceWarning \
  -m unittest discover -s tests -p 'test_*.py' -v
.venv/bin/python -m compileall -q src tests
```

`DPM_TEST_WHEEL` 必须指向本次构建的 wheel，才能执行实际离线安装测试。临时文件遵守 `TMPDIR`。少数真实权限边界测试需要 Linux root 权限，普通用户运行时会明确跳过；这与断言通过不同。仅在可信测试环境中提升权限，不要为通过测试放宽服务的隔离设置。

## Docker 与部署文件

```sh
docker compose -f packaging/docker/compose.yaml config --quiet
docker compose -f packaging/docker/compose.yaml -f packaging/docker/compose.setup.yaml config --quiet
docker build -f packaging/docker/Dockerfile -t douyin-presence-monitor:0.1.0 .
docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges:true douyin-presence-monitor:0.1.0 --version
```

容器使用 UID/GID 10001，入口从根内版本环境加载程序。上述命令验证构建、配置及离线入口，不访问真实平台。原生安装器的 `--dry-run` 不创建账号、注册服务或启动监测；真正部署仍需按 README 完成初始化和权限核验。

## GitHub Actions

[测试与构建工作流](https://github.com/xxvcc/douyin-presence-monitor/actions) 在 Python 3.11、3.12、3.13 上构建并测试软件，同时验证 Docker 构建和离线入口。Python 作业先构建 wheel，再将该 wheel 传给安装回归测试。

查看具体提交对应的运行记录和跳过原因，不将其他提交的绿色状态当作当前代码已经验证。Actions 日志与构建产物只应包含合成测试数据和可公开的发行内容。

## 不作出的保证

- 抖音接口属于网页内部接口，测试通过不代表其永久可用，也不保证任意账号关系都能查询。
- 最近活跃不是精确上线、退出、连续在线时长或消息已读证据。
- Telegram 接口接受消息不等于收件人已读；网络超时后的重试可能重复投递。
- 独立日志按需创建，尚无警告或错误时可能没有 error.log。
- 备份校验用于发现损坏，不代替可信来源验证、磁盘加密或访问控制。
