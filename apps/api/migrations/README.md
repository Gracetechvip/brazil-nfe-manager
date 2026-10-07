# 数据库迁移

当前首版以 `Base.metadata.create_all()` 作为空环境初始化迁移，服务启动时只创建不存在的表，不删除或重置已有数据。生产升级前应先完成完整备份，再将新增 SQLAlchemy 模型转换为正式 Alembic migration；此目录保留迁移版本位置，避免把 `create_all` 当作破坏性升级工具。

部署启动命令：

```bash
docker compose run --rm api python -c "from app.db import Base,engine; Base.metadata.create_all(engine)"
```
