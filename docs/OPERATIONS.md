# 运行、备份与恢复操作说明

本文描述 V1 当前仓库可以验证的操作。Docker Compose 的 `ROOT` 路径必须由部署人员按飞牛实际存储池替换；本地测试可以继续使用仓库 `.env` 的 `data-local`。数据库服务只加入 Compose 内部网络，宿主机不映射 PostgreSQL 端口。

## 启动与升级

本地已有 `.env` 时保留现有密码和 ROOT 配置。只有第一次新部署时才复制环境模板。在仓库目录执行：

```powershell
Copy-Item .env.example .env
# 编辑 .env：设置随机 POSTGRES_PASSWORD、ROOT 和 WEB_PORT
docker compose up -d --build
docker compose ps
```

首次管理员使用登录页的初始化流程。升级前先完成一次备份并确认校验成功，再拉取新源码、重新构建并启动；迁移脚本（若版本包含）必须先执行，升级后核对公司数、发票数、事件数、金额及原文对象数。不要使用 `docker compose down -v`，它会删除数据库卷。

## 备份

管理员在系统管理页创建备份（`POST /api/backups`）。任务持有独立 PostgreSQL 会话级 advisory lock，导入、分类和状态变更会等待该锁；业务 Session 提交不释放锁，嵌套服务可重入，锁连接使用独立 NullPool 避免占满业务连接池。备份包位于 `BACKUPS_DIR`，文件为一个 ZIP，包含：

- `database.sqlite`（开发 SQLite）或 `database.dump`（PostgreSQL custom format）；
- `objects/` 下所有数据库登记的原始 XML/事件对象；
- `staging/` 下排队、处理中及失败导入任务引用的可重试原始上传文件；
- `manifest.json`、`config/settings.json`；
- 全部表计数、生产环境月度与公司月度销售 Decimal 汇总、每个原文对象与待重试上传文件的 SHA-256 和大小；
- 规则、解析器/规则版本和已安装依赖版本（不保存环境变量原文或数据库凭证）。

备份仅在数据库快照、原文复制、清单生成、重新打开 ZIP 和全部哈希检查都成功后才原子改名为完成包。任何失败会将 `BackupRun.status` 设为 `failed` 并记录不含凭证的错误摘要；生产数据库和原文对象不会被删除或覆盖。配置清单不保存数据库 URL、密码、Cookie 或环境变量原文。

建议每日完整备份、每周和每月保留策略由管理员按容量配置。`BACKUPS_DIR` 所在磁盘不是独立备份，必须另行复制到另一块磁盘或另一台 NAS，并检查复制结果。备份失败、磁盘不足和外部副本过期应纳入值班检查。

## 恢复演练与校验

管理员对一个已完成任务运行隔离验证（`POST /api/backups/{id}/verify`）。验证过程：

1. 在临时目录安全解压并拒绝绝对路径、`..` 路径；
2. 校验归档自身 SHA-256、数据库/配置快照以及全部原文和待重试上传的 SHA-256/大小；
3. SQLite 在临时数据库运行 `integrity_check`；PostgreSQL 创建随机 `nfe_restore_*` 空数据库并 `pg_restore --exit-on-error`；
4. 在恢复库核对全部表计数、生产月份金额、公司月份金额、数据库原文清单、所有外键及待重试上传引用；
5. 从恢复库选最多 20 张生产发票的主原文版本，在隔离目录导出 ZIP，并逐文件核对原始字节哈希；
6. 关闭隔离连接并删除本次随机创建的临时库，成功才更新 `last_restore_test`、恢复证据与耗时，失败记录审计错误。生产库不被覆盖。

首次上线和每月至少一次在独立目录/空环境演练，并保存演练时间、耗时、公司数、票数、事件数、金额和原文清单。验证时没有发票原文版本会记录“无可抽样原文”，不会宣称 XML 导出已验证。数据库用户需具备创建/删除临时数据库的权限；权限不足会使演练明确失败。

## 手工灾难恢复到新目标

安全入口提供“新目录 + 新数据库”的恢复，不提供覆盖生产或 `--force` 选项。运行前保存备份任务中 `details.archive_sha256`，确保它与备份包一起保留在独立目标；这个外部哈希可发现备份包自身被改写。恢复目录必须尚不存在，不能放进现有 objects/staging/exports/backups 下，也不能指向这些目录的父目录。

SQLite 开发备份示例：

```powershell
cd apps/api
py -3 -m app.services.backup restore --archive "ABSOLUTE_BACKUP_ZIP" --target-dir "NEW_RECOVERY_DIRECTORY" --expected-sha256 "SAVED_ARCHIVE_SHA256"
```

PostgreSQL/飞牛示例：先准备新的空 PostgreSQL 服务及独立数据库数据卷，将恢复任务的 `DATABASE_URL` 配置为该服务已有的管理数据库（凭证在环境文件提供）。恢复数据库名必须是新的 `nfe_recovered_*`，不能是当前 `DATABASE_URL` 的库名。目标目录需要单独挂载，不能留在临时容器可写层：

```text
docker compose run --rm --no-deps --volume "ACTUAL_INDEPENDENT_RECOVERY_PATH:/recovery" api python -m app.services.backup restore --archive /var/lib/nfe/backups/ACTUAL_BACKUP.zip --target-dir /recovery/recovered-20261008 --database-name nfe_recovered_20261008 --expected-sha256 SAVED_ARCHIVE_SHA256
```

该命令只有在全清单、计数、金额、引用和样本导出全部通过后才发布新恢复目录，保存 `restore-report.json`。失败会清理本次新建的恢复库；已有生产数据库和目录不允许被覆盖。恢复完成后先用隔离 Web/API 配置指向新库与恢复目录，核对业务结果；管理员确认后再切换业务入口，并保留原环境。对正在运行的业务，先停止写入后再切换。

## 飞牛部署前检查

记录 CPU 架构（x86_64/ARM）、Docker/Compose 版本、内存、存储池绝对路径和剩余空间、局域网 IP、端口、独立备份目标以及对象/备份读写权限。API 镜像使用 PostgreSQL 时必须包含 `pg_dump`、`pg_restore`、`createdb` 和 `dropdb`；缺少客户端会使 PostgreSQL 备份明确失败，而不会生成假成功包。

## 已知限制

当前备份测试使用独立 SQLite 合成数据，已验证完整恢复、月份口径、原文/归档篡改、待重试上传、样本导出、目录穿越、禁止覆盖和磁盘满失败；真实本地 PostgreSQL 备份/演练的证据以测试报告为准。独立 NAS 复制、真实飞牛性能和 RPO/RTO 必须在目标硬件演练后填写结果。V1 不开具发票、不自动报税、不连接 SEFAZ、不做平台同步或利润计算。
