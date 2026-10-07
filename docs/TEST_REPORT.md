# 测试报告

执行时间：2026-10-08（本地 Windows Docker，非飞牛 NAS）。

## 自动化证据

```text
py -3 -m pytest -q apps/api --disable-warnings
22 passed, 376 warnings
npm run build --prefix apps/web
通过（tsc -b + vite build）
py -3 -m compileall -q apps/api/app
通过
```

22 个 API/业务用例在隔离临时 SQLite 中运行，覆盖授权证据、原始字节去重、同票冲突隔离、裸票补授权、取消事件先后到达、销售/采购退货、采购不计销售、日期/ISO 周、SKU 与商品名同一行、测试环境隔离、全结果导出哈希、权限撤销、跨站写入、磁盘满原子失败、重试、任务重启恢复、并发重复和原文引用。`test_backup.py` 覆盖 5 个备份/恢复与锁用例。

## 本地 Docker 证据

- `docker compose up -d --build` 成功，`db`、`api`、`worker`、`web` 均运行；Web 映射 `localhost:18180`，PostgreSQL 没有宿主端口映射。
- `GET /api/health/live` 返回 `{"status":"ok"}`；`GET /api/health/ready` 返回 `{"status":"ready"}`。
- 登录后真实数据库：1 个公司、1 个导入批次、46 张 Model 55 XML；原文 `vNF` 合计 BRL 13,627.30；46 张均为生产环境、授权证据，业务分类待确认，因此仪表盘销售额 BRL 0.00、待复核金额 BRL 13,627.30。
- 管理员真实 API 创建备份 #2 成功，并执行 `POST /api/backups/2/verify`：`verified`，46 个原文对象、46 张发票，隔离 PostgreSQL 恢复验证耗时约 2.552 秒。使用与 PostgreSQL 16 数据库匹配的 `pg_dump/pg_restore`。
- 页面登录和仪表盘人工浏览器验收通过：左侧七页导航、中文工作台、日期/公司筛选、待复核提示和 R$ 0,00 销售卡片可见。自动化浏览器扩展连接不稳定，使用本地内嵌浏览器完成页面状态核对。

## 真实样本对账

附件中的脱敏 Model 55 样本：47 个成员，其中 46 个 XML；发行方身份已移除；日期 2026-07-04 至 2026-07-30；46 张 `cStat=100`、生产环境；CFOP 5105（21 张）和 6105（25 张）；金额仅作为本地验证数据，不在公开报告中保留。文件原文哈希已入库并可从发票详情下载。

5105/6105 是否属于销售由业务负责人确认后才能批量分类，当前没有把它们自动计入销售额，也没有声称完成税务分类核实。

## 未完成的目标环境验证

尚未访问或部署飞牛 OS；CPU 架构、实际存储池绝对路径、局域网 IP、18180 冲突、磁盘满行为、独立备份目的地和 NAS 恢复演练待用户提供并现场验证。当前 Docker 使用本地 `data-local`，不可作为独立备份。真实 PostgreSQL 隔离恢复已在本地 Docker 完成，不等于飞牛硬件恢复。

已知限制：V1 不开具发票、不自动报税、不连接 SEFAZ、不做平台同步或利润。未提供取消事件样本与完整退货样本前，事件/退货分类仍需真实业务验收。

