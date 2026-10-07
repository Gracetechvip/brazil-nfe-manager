from datetime import datetime, date
from decimal import Decimal
from typing import Optional
from sqlalchemy import String, Text, Boolean, DateTime, Date, ForeignKey, Numeric, Integer, UniqueConstraint, Index, JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .db import Base


class Company(Base):
    __tablename__ = "companies"
    id: Mapped[int] = mapped_column(primary_key=True)
    cnpj: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    legal_name: Mapped[str] = mapped_column(String(255), default="")
    alias: Mapped[str] = mapped_column(String(255), default="")
    uf: Mapped[Optional[str]] = mapped_column(String(2), nullable=True)
    timezone: Mapped[str] = mapped_column(String(64), default="America/Sao_Paulo")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    confirmed: Mapped[bool] = mapped_column(Boolean, default=False)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    login: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    display_name: Mapped[str] = mapped_column(String(255), default="")
    role: Mapped[str] = mapped_column(String(30), default="finance")
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Session(Base):
    __tablename__ = "sessions"
    token: Mapped[str] = mapped_column(String(128), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime)


class UserCompanyScope(Base):
    __tablename__ = "user_company_scopes"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"))
    permissions: Mapped[str] = mapped_column(String(255), default="view,import,classify,export")
    __table_args__ = (UniqueConstraint("user_id", "company_id"),)


class ImportBatch(Base):
    __tablename__ = "import_batches"
    id: Mapped[int] = mapped_column(primary_key=True)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    target_company_id: Mapped[Optional[int]] = mapped_column(ForeignKey("companies.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="processing")
    counters: Mapped[dict] = mapped_column(JSON, default=dict)
    report: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    parser_version: Mapped[str] = mapped_column(String(30), default="1.0")
    upload_key: Mapped[Optional[str]] = mapped_column(String(180), unique=True, nullable=True)


class ImportEntry(Base):
    __tablename__ = "import_entries"
    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("import_batches.id", ondelete="CASCADE"), index=True)
    member_path: Mapped[str] = mapped_column(String(500))
    object_id: Mapped[Optional[int]] = mapped_column(ForeignKey("file_objects.id"), nullable=True)
    result: Mapped[str] = mapped_column(String(30))
    error_code: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    invoice_id: Mapped[Optional[int]] = mapped_column(ForeignKey("invoices.id"), nullable=True)
    event_id: Mapped[Optional[int]] = mapped_column(ForeignKey("invoice_events.id"), nullable=True)


class FileObject(Base):
    __tablename__ = "file_objects"
    id: Mapped[int] = mapped_column(primary_key=True)
    sha256: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    size_bytes: Mapped[int] = mapped_column(Integer)
    relative_path: Mapped[str] = mapped_column(String(500))
    original_name: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Invoice(Base):
    __tablename__ = "invoices"
    id: Mapped[int] = mapped_column(primary_key=True)
    environment: Mapped[str] = mapped_column(String(10), default="production")
    access_key: Mapped[str] = mapped_column(String(64), index=True)
    model: Mapped[str] = mapped_column(String(5), default="55")
    issuer_cnpj: Mapped[str] = mapped_column(String(32), default="")
    recipient_cnpj: Mapped[str] = mapped_column(String(32), default="")
    number: Mapped[str] = mapped_column(String(30), default="")
    series: Mapped[str] = mapped_column(String(30), default="")
    issue_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    issue_text: Mapped[str] = mapped_column(String(80), default="")
    issue_local_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True, index=True)
    tp_nf: Mapped[str] = mapped_column(String(5), default="")
    fin_nfe: Mapped[str] = mapped_column(String(5), default="")
    fiscal_status: Mapped[str] = mapped_column(String(30), default="pending_evidence", index=True)
    v_nf: Mapped[Decimal] = mapped_column(Numeric(20, 2), default=0)
    v_prod: Mapped[Decimal] = mapped_column(Numeric(20, 2), default=0)
    v_desc: Mapped[Decimal] = mapped_column(Numeric(20, 2), default=0)
    parser_version: Mapped[str] = mapped_column(String(30), default="1.0")
    conflict: Mapped[bool] = mapped_column(Boolean, default=False)
    review_reason: Mapped[str] = mapped_column(Text, default="")
    extra_json: Mapped[dict] = mapped_column(JSON, default=dict)
    __table_args__ = (UniqueConstraint("environment", "access_key"), Index("ix_invoice_identity", "environment", "access_key"))


class InvoiceCompanyRole(Base):
    __tablename__ = "invoice_company_roles"
    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id", ondelete="CASCADE"), index=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(20))
    business_class: Mapped[str] = mapped_column(String(30), default="pending")
    review_state: Mapped[str] = mapped_column(String(30), default="pending")
    rule_version: Mapped[str] = mapped_column(String(30), default="1.0")
    included: Mapped[bool] = mapped_column(Boolean, default=False)
    internal_trade: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = (UniqueConstraint("invoice_id", "company_id", "role"),)


class InvoiceItem(Base):
    __tablename__ = "invoice_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id", ondelete="CASCADE"), index=True)
    line_no: Mapped[str] = mapped_column(String(20))
    c_prod: Mapped[str] = mapped_column(String(255), default="")
    x_prod: Mapped[str] = mapped_column(Text, default="")
    ncm: Mapped[str] = mapped_column(String(30), default="")
    cfop: Mapped[str] = mapped_column(String(10), default="")
    u_com: Mapped[str] = mapped_column(String(30), default="")
    q_com: Mapped[Decimal] = mapped_column(Numeric(20, 8), default=0)
    v_un_com: Mapped[Decimal] = mapped_column(Numeric(20, 8), default=0)
    v_prod: Mapped[Decimal] = mapped_column(Numeric(20, 2), default=0)
    v_desc: Mapped[Decimal] = mapped_column(Numeric(20, 2), default=0)
    extra_json: Mapped[dict] = mapped_column(JSON, default=dict)
    __table_args__ = (UniqueConstraint("invoice_id", "line_no"),)


class InvoiceFileVersion(Base):
    __tablename__ = "invoice_file_versions"
    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id", ondelete="CASCADE"), index=True)
    object_id: Mapped[int] = mapped_column(ForeignKey("file_objects.id"))
    document_kind: Mapped[str] = mapped_column(String(30), default="nfe")
    evidence_level: Mapped[str] = mapped_column(String(30), default="raw")
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)
    imported_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class InvoiceEvent(Base):
    __tablename__ = "invoice_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[Optional[int]] = mapped_column(ForeignKey("invoices.id", ondelete="SET NULL"), nullable=True, index=True)
    environment: Mapped[str] = mapped_column(String(10), default="production")
    access_key: Mapped[str] = mapped_column(String(64), index=True)
    event_type: Mapped[str] = mapped_column(String(20))
    sequence: Mapped[str] = mapped_column(String(10), default="1")
    event_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    receipt_status: Mapped[str] = mapped_column(String(20), default="unknown")
    protocol: Mapped[str] = mapped_column(String(80), default="")
    accepted: Mapped[bool] = mapped_column(Boolean, default=False)
    raw_json: Mapped[dict] = mapped_column(JSON, default=dict)
    __table_args__ = (UniqueConstraint("environment", "access_key", "event_type", "sequence"),)


class EventFileVersion(Base):
    __tablename__ = "event_file_versions"
    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("invoice_events.id", ondelete="CASCADE"), index=True)
    object_id: Mapped[int] = mapped_column(ForeignKey("file_objects.id"))
    kind: Mapped[str] = mapped_column(String(30), default="event")
    imported_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class InvoiceTax(Base):
    __tablename__ = "invoice_taxes"
    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id", ondelete="CASCADE"), index=True)
    item_id: Mapped[Optional[int]] = mapped_column(ForeignKey("invoice_items.id", ondelete="CASCADE"), nullable=True, index=True)
    scope: Mapped[str] = mapped_column(String(20), default="item")
    tax_type: Mapped[str] = mapped_column(String(30), default="")
    cst_csosn: Mapped[str] = mapped_column(String(30), default="")
    base: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True)
    rate: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 8), nullable=True)
    amount: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True)
    raw_json: Mapped[dict] = mapped_column(JSON, default=dict)


class InvoiceReference(Base):
    __tablename__ = "invoice_references"
    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id", ondelete="CASCADE"), index=True)
    referenced_key: Mapped[str] = mapped_column(String(64), index=True)
    relationship_type: Mapped[str] = mapped_column(String(30), default="referenced")


class ClassificationRule(Base):
    __tablename__ = "classification_rules"
    id: Mapped[int] = mapped_column(primary_key=True)
    version: Mapped[str] = mapped_column(String(30), default="1.0")
    company_id: Mapped[Optional[int]] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), nullable=True, index=True)
    conditions_json: Mapped[dict] = mapped_column(JSON, default=dict)
    result_class: Mapped[str] = mapped_column(String(30), default="pending")
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class CoveragePeriod(Base):
    __tablename__ = "coverage_periods"
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"))
    month: Mapped[str] = mapped_column(String(7))
    coverage_state: Mapped[str] = mapped_column(String(30), default="not_imported")
    confirmed_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)
    confirmed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    __table_args__ = (UniqueConstraint("company_id", "month"),)


class ExportJob(Base):
    __tablename__ = "export_jobs"
    id: Mapped[int] = mapped_column(primary_key=True)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    filters_json: Mapped[dict] = mapped_column(JSON, default=dict)
    result_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(30), default="queued")
    file_count: Mapped[int] = mapped_column(Integer, default=0)
    archive_path: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class ExportJobFile(Base):
    __tablename__ = "export_job_files"
    id: Mapped[int] = mapped_column(primary_key=True)
    export_job_id: Mapped[int] = mapped_column(ForeignKey("export_jobs.id", ondelete="CASCADE"))
    invoice_id: Mapped[Optional[int]] = mapped_column(ForeignKey("invoices.id"), nullable=True)
    event_id: Mapped[Optional[int]] = mapped_column(ForeignKey("invoice_events.id"), nullable=True)
    object_id: Mapped[int] = mapped_column(ForeignKey("file_objects.id"))
    zip_path: Mapped[str] = mapped_column(String(500))


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)
    action: Mapped[str] = mapped_column(String(80))
    entity_type: Mapped[str] = mapped_column(String(80))
    entity_id: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    before_json: Mapped[dict] = mapped_column(JSON, default=dict)
    after_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class BackupRun(Base):
    __tablename__ = "backup_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    snapshot_id: Mapped[str] = mapped_column(String(100), unique=True)
    start_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    end_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="running")
    manifest_path: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    external_status: Mapped[str] = mapped_column(String(30), default="not_configured")
    last_restore_test: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    details: Mapped[dict] = mapped_column(JSON, default=dict)


class DurableTask(Base):
    __tablename__ = "durable_tasks"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(20))
    entity_id: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(30), default="queued", index=True)
    error: Mapped[str] = mapped_column(Text, default="")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    __table_args__ = (UniqueConstraint("kind", "entity_id"),)


class SavedFilter(Base):
    __tablename__ = "saved_filters"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    name: Mapped[str] = mapped_column(String(100))
    filters: Mapped[dict] = mapped_column(JSON, default=dict)


class SchemaMigration(Base):
    __tablename__ = "schema_migrations"
    version: Mapped[str] = mapped_column(String(80), primary_key=True)
    applied_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
