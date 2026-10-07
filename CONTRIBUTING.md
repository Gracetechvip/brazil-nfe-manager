# Contributing

感谢关注 Brazil NF-e Manager。提交问题或改动前，请先阅读 `docs/PRD_CALIBRATION.md`、`docs/DATA_MODEL.md` 和 `docs/OPERATIONS.md`。

## 开发约定

- 不提交真实 CNPJ、访问键、买方资料、原始 XML、ZIP、备份或数据库文件。
- 新增统计逻辑必须说明日期、金额、退货和去重口径，并补充测试。
- 需要保持原始 XML 字节不可变，任何解析失败都必须可审计。
- 提交前运行 API 测试、前端构建和 Python 编译检查。

## Issue

请提供可复现步骤、运行环境、日志中的脱敏片段和预期行为。不要上传真实发票文件。
