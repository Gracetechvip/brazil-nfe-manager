# Brazil NF-e Manager

Brazil NF-e Manager 是一个面向公司内网和飞牛 OS（FnOS）的巴西电子发票管理系统。它用于导入、长期保存、检索和统计 NF-e Model 55 XML，并保留原始 XML、SHA-256 哈希、导入批次、票据事件和审计记录。

> 当前公开仓库是 **v1 基线**。后续 v2 需求开发在本地独立分支进行，不会自动发布到本仓库。

## 功能

- XML/ZIP 异步导入，支持导入批次、重复识别和冲突隔离
- 原始 XML 不可变保存，保存文件对象、版本和 SHA-256 哈希
- CNPJ、访问键和事件身份按字符串处理
- 公司范围权限、角色权限、登录、审计日志和异常审查
- 按开票当地日期统计日、ISO 周、月、年销售额
- 按日期、金额、SKU、产品名和业务分类筛选发票
- 保持整票粒度，筛选商品行时仍可原样导出整张 XML
- 异步查询结果 ZIP 导出、manifest 和原文哈希校验
- PostgreSQL 持久化、独立 worker、Docker Compose 和 Nginx
- 完整备份、备份校验和隔离恢复演练支持

系统明确不负责发票开具、SEFAZ 联机查询、自动报税、平台同步和利润计算。

## 技术栈

- Frontend: React + TypeScript + Vite
- API: FastAPI + Python
- Database: PostgreSQL 16
- Worker: 独立 Python worker 进程
- Web gateway: Nginx
- Runtime: Docker Compose

## 快速开始

### Docker Compose

```bash
cp .env.example .env
# 编辑 .env，至少设置一个随机的 POSTGRES_PASSWORD
docker compose up -d --build
```

默认 Web 端口为 `8080`。如需在本机使用 `18180`：

```dotenv
WEB_PORT=18180
```

然后访问 `http://localhost:18180`。数据库只在 Compose 内部网络中运行，不映射宿主端口。

首次启动后，通过 `POST /api/auth/bootstrap` 创建第一个管理员：

```json
{"login":"admin","password":"change-this-password","display_name":"管理员"}
```

生产环境请使用高强度密码，并在首次登录后按公司范围创建用户。

### 本地开发

API：

```powershell
python -m venv .venv
.\\.venv\\Scripts\\Activate.ps1
pip install -r apps/api/requirements.txt
$env:DATABASE_URL = "sqlite:///./data/nfe.db"
uvicorn app.main:app --app-dir apps/api --reload
```

前端：

```powershell
cd apps/web
npm ci
npm run dev
```

生产和局域网部署建议使用 PostgreSQL Compose 配置，不要把 SQLite 作为正式数据库。

## 飞牛 OS 部署

将项目目录放在飞牛可读写的位置，复制 `.env.example` 为 `.env`，设置：

- `ROOT`：持久化数据目录，例如 `/vol1/1000/nfe-data`
- `WEB_PORT`：局域网访问端口
- `POSTGRES_PASSWORD`：随机数据库密码
- `APP_TIMEZONE`：公司业务所在地时区，默认 `America/Sao_Paulo`

`ROOT` 下会创建 `db`、`objects`、`exports`、`backups` 和 `staging`。容器重建不会清空这些目录；不要使用 `docker compose down -v`。

首次部署前应验证飞牛 CPU 架构、内存、磁盘空间、局域网 IP、端口和独立备份目标。

## 测试

```powershell
py -3 -m pytest -q apps/api
npm run build --prefix apps/web
py -3 -m compileall -q apps/api/app
```

测试覆盖 XML/ZIP 安全解析、身份去重、事件处理、权限、统计、导出、备份和恢复等核心行为。真实生产 XML 的业务分类仍需由财务使用脱敏样本验收，测试数据不能代替税务确认。

## 文档

- [PRD 校准](docs/PRD_CALIBRATION.md)
- [页面交互说明](docs/UI_INTERACTION.md)
- [数据模型](docs/DATA_MODEL.md)
- [部署、升级、备份和恢复](docs/OPERATIONS.md)
- [测试报告](docs/TEST_REPORT.md)
- [任务清单](docs/TASKS.md)
- [真实样本审计摘要](docs/real-sample-audit.json)

## 数据和安全边界

- 不要提交 `.env`、数据库文件、原始 XML、ZIP、备份或本地数据目录。
- `CNPJ`、访问键和事件 ID 不能当作整数处理。
- 原始 XML 不修改、不重新签名，导出时使用原始字节。
- 销售统计必须以公司角色、业务分类、授权证据和取消事件共同决定。
- 缺少真实样本或不确定 CFOP 时，系统应进入待确认状态，而不是猜测税务分类。

## License

当前仓库尚未声明开源许可证。未经项目所有者书面许可，请勿将其作为生产软件或重新发布。
