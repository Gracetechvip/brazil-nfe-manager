# 数据模型

核心表由 SQLAlchemy 模型实现：`companies`、`users`、`sessions`、`user_company_scopes`、`import_batches`、`import_entries`、`file_objects`、`invoices`、`invoice_company_roles`、`invoice_items`、`invoice_file_versions`、`invoice_events`、`coverage_periods`、`export_jobs`、`export_job_files`、`audit_logs`、`backup_runs`。

金额使用 `NUMERIC(20,2)`，数量和单价使用 `NUMERIC(20,8)`；访问键和 CNPJ 使用字符串。`environment + access_key`、文件 SHA-256、事件身份和商品行号均有唯一约束。原始 XML 存放于对象目录，数据库仅保存不可变对象引用和哈希。
