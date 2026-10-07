import { FormEvent, useEffect, useState } from 'react';

type Page = 'dashboard' | 'companies' | 'imports' | 'invoices' | 'filter' | 'exports' | 'settings';
type Scope = { company_id: number; permissions: string[] };
type User = { id: number; login: string; display_name: string; role: string; scopes?: Scope[]; active?: boolean };
type Company = { id: number; cnpj: string; legal_name: string; alias: string; uf?: string; confirmed: boolean; timezone: string; invoice_count?: number; total_sales?: string; pending_count?: number };
type Summary = { sales_amount: string; returns_amount: string; net_amount: string; pending_amount: string; invoice_count: number; pending_count: number; coverage_note: string; all_history_amount?: string; history_start?: string };
type Trend = { period: string; amount: string };
type Invoice = { id: number; access_key: string; environment: string; model: string; issuer_cnpj: string; recipient_cnpj: string; number: string; series?: string; issue_local_date: string | null; fiscal_status: string; v_nf: string; v_prod: string; conflict: boolean; roles: { company_id: number; role: string; business_class: string; included: boolean }[]; items: { line_no: number; c_prod: string; x_prod: string; ncm?: string; cfop?: string; q_com: string; v_prod: string }[] };
type ImportBatch = { id: number; status: string; counters: Record<string, number>; created_at: string; finished_at?: string; report?: { sales_adjustment?: string; net_adjustment?: string } };
type ExportJob = { id: number; status: string; file_count?: number; snapshot?: { invoice_count?: number; created_at?: string }; created_at: string; completed_at?: string };
type Backup = { id: number; snapshot_id: string; status: string; manifest_path: string; start_at: string; end_at?: string };
type InvoiceFilters = { search?: string; date_from?: string; date_to?: string; company_id?: string; min_amount?: string; max_amount?: string; sku?: string; sku_exact?: string; product_name?: string; ncm?: string; cfop?: string; status?: string; business_class?: string; environment?: string };
type ExportPreset = { filters: InvoiceFilters; invoice_ids?: number[]; all_results?: boolean; total?: number };
type SavedFilter = { id: number; name: string; filters_json?: InvoiceFilters; filters?: InvoiceFilters };
const can = (user: User, permission: string, companyId?: string | number) => user.role === 'admin' || Boolean(user.scopes?.some(scope => (!companyId || scope.company_id === Number(companyId)) && scope.permissions.some(p => p === permission || p === 'manage' || (permission === 'classify' && p === 'classification'))));
const classes = Object.entries({ sale: '销售', sales_return: '销售退货', purchase: '采购', purchase_return: '采购退货', import: '进口', transfer: '调拨', gift: '赠送', pending: '待确认', other: '其他' });

const money = (value: string | number) => new Intl.NumberFormat('pt-BR', { style: 'currency', currency: 'BRL' }).format(Number(value || 0));
const dateTime = (value?: string) => value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '—';
const dateOnly = (value?: string | null) => value ? value.slice(0, 10) : '—';
const statusLabel: Record<string, string> = { authorized: '已授权', pending_evidence: '待授权证据', conflict: '冲突待审', canceled: '已取消', processing: '处理中', running: '处理中', partial: '部分完成', completed: '已完成', queued: '排队中', failed: '失败' };
const businessLabel: Record<string, string> = { sale: '销售', sales_return: '销售退货', purchase: '采购', purchase_return: '采购退货', import: '进口', transfer: '调拨', gift: '赠送', pending: '待确认', other: '其他', internal: '内部交易' };

async function api<T = any>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(path, { credentials: 'include', ...init });
  const text = await response.text();
  let payload: any = null;
  try { payload = text ? JSON.parse(text) : null; } catch { payload = text; }
  if (!response.ok) throw new Error(payload?.detail || payload?.message || response.statusText || '请求失败');
  return payload as T;
}
const errorMessage = (error: unknown) => error instanceof Error ? error.message : '操作失败，请重试';
const jsonRequest = (method: string, body: unknown): RequestInit => ({ method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });

function Login({ onLogin }: { onLogin: (user: User) => void }) {
  const [login, setLogin] = useState('admin'); const [password, setPassword] = useState(''); const [error, setError] = useState(''); const [busy, setBusy] = useState(false);
  async function submit(event: FormEvent) { event.preventDefault(); setBusy(true); setError(''); try { await api<User>('/api/auth/login', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ login, password }) }); onLogin(await api<User>('/api/me')); } catch (e) { setError(e instanceof Error ? e.message : '登录失败'); } finally { setBusy(false); } }
  return <div className="login-page"><div className="login-card"><div className="brand-mark">NF</div><p className="kicker">BRAZIL · NF-e MANAGER</p><h1>登录发票管理系统</h1><p className="muted">集中管理 XML、发票分类与开票统计</p><form onSubmit={submit}><label>账号 / Login<input autoFocus value={login} onChange={e => setLogin(e.target.value)} autoComplete="username" /></label><label>密码 / Senha<input type="password" value={password} onChange={e => setPassword(e.target.value)} autoComplete="current-password" /></label>{error && <div className="alert error">{error}</div>}<button className="button primary wide" disabled={busy}>{busy ? '正在登录…' : '登录系统'}</button></form><p className="login-hint">首次使用请由管理员调用 <code>/api/auth/bootstrap</code> 创建账号。</p></div></div>;
}

function App() {
  const [user, setUser] = useState<User | null>(null); const [checking, setChecking] = useState(true); const [page, setPage] = useState<Page>('dashboard'); const [exportPreset, setExportPreset] = useState<ExportPreset>({ filters: {}, all_results: true });
  useEffect(() => { api<User>('/api/me').then(setUser).catch(() => setUser(null)).finally(() => setChecking(false)); }, []);
  if (checking) return <div className="loading"><div className="spinner" />正在连接系统…</div>;
  if (!user) return <Login onLogin={setUser} />;
  const openExport = (preset: ExportPreset) => { setExportPreset(preset); setPage('exports'); };
  return <Shell user={user} page={page} setPage={setPage} exportPreset={exportPreset} openExport={openExport} logout={async () => { await api('/api/auth/logout', { method: 'POST' }).catch(() => undefined); setUser(null); }} />;
}

const pageLabels: Record<Page, { title: string; sub: string; icon: string }> = {
  dashboard: { title: '仪表盘', sub: '销售概览与资料覆盖', icon: '▦' }, companies: { title: '公司管理', sub: 'CNPJ、公司范围与时区', icon: '▤' }, imports: { title: 'XML 导入', sub: '导入原始票据并查看批次', icon: '⇧' }, invoices: { title: '发票中心', sub: '票据明细、分类与状态', icon: '▤' }, filter: { title: 'XML 筛选', sub: '按业务条件查找原始票据', icon: '⌕' }, exports: { title: 'XML 导出', sub: '任务、快照与下载', icon: '⇩' }, settings: { title: '系统管理', sub: '备份、审计与运行信息', icon: '⚙' },
};

function Shell({ user, page, setPage, logout, exportPreset, openExport }: { user: User; page: Page; setPage: (page: Page) => void; logout: () => void; exportPreset: ExportPreset; openExport: (preset: ExportPreset) => void }) {
  const pages = (['dashboard', 'companies', 'imports', 'invoices', 'filter', 'exports', 'settings'] as Page[]).filter(item => (item !== 'imports' || can(user, 'import')) && (item !== 'exports' || can(user, 'export')));
  return <div className="app-shell">
    <aside className="sidebar"><div className="brand"><span className="brand-mark small">NF</span><div><strong>NF-e Manager</strong><small>巴西发票管理</small></div></div><div className="side-section-label">工作台</div><nav>{pages.map(item => <button key={item} className={`nav-item ${page === item ? 'active' : ''}`} onClick={() => setPage(item)}><span className="nav-icon">{pageLabels[item].icon}</span><span>{pageLabels[item].title}</span></button>)}</nav><div className="sidebar-bottom"><div className="online"><span className="online-dot" /> 已连接本地服务</div><div className="user-mini"><div className="avatar">{(user.display_name || user.login).slice(0, 1).toUpperCase()}</div><div><b>{user.display_name || user.login}</b><small>{user.role === 'admin' ? '管理员' : '职员'}</small></div><button aria-label="退出登录" title="退出登录" onClick={logout}>↪</button></div></div></aside>
    <main className="main-content"><header className="page-header"><div><p className="breadcrumb">工作台 / {pageLabels[page].title}</p><h1>{pageLabels[page].title}</h1><p className="page-subtitle">{pageLabels[page].sub}</p></div><div className="header-right"><span className="scope-chip">BRL · 开票当地日期</span><button className="icon-button" onClick={() => setPage('settings')} title="系统管理">⚙</button></div></header><div className="page-body">
      {page === 'dashboard' && <Dashboard setPage={setPage} user={user} />}
      {page === 'companies' && <Companies user={user} />}
      {page === 'imports' && can(user, 'import') && <Imports user={user} />}
      {(page === 'invoices' || page === 'filter') && <InvoiceExplorer key={page} user={user} initialLoad={page === 'invoices'} onExport={openExport} />}
      {page === 'exports' && can(user, 'export') && <Exports user={user} preset={exportPreset} />}
      {page === 'settings' && <Settings user={user} />}
    </div></main>
  </div>;
}

function Dashboard({ setPage, user }: { setPage: (page: Page) => void; user: User }) {
  const [companies, setCompanies] = useState<Company[]>([]);
  const [summary, setSummary] = useState<Summary | null>(null);
  const [trend, setTrend] = useState<Trend[]>([]);
  const [ranking, setRanking] = useState<{ company_id: number; cnpj: string; legal_name: string; amount: string }[]>([]);
  const [filters, setFilters] = useState({ date_from: '', date_to: '', company_id: '' });
  const [appliedFilters, setAppliedFilters] = useState(filters);
  const [granularity, setGranularity] = useState('month');
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState('');
  async function load(scope = filters, unit = granularity) {
    setBusy(true); setError('');
    try {
      if (scope.date_from && scope.date_to && scope.date_from > scope.date_to) throw new Error('开始日期不能晚于结束日期。');
      const params = buildQuery(scope);
      const [list, s, t, ranks] = await Promise.all([api<Company[]>('/api/companies'), api<Summary>(`/api/stats/summary?${params}`), api<Trend[]>(`/api/stats/trends?${params}&granularity=${unit}`), api<typeof ranking>(`/api/stats/companies?${params}`)]);
      setCompanies(list); setSummary(s); setTrend(t); setRanking(ranks); setAppliedFilters(scope);
    } catch (e) { setError(errorMessage(e)); } finally { setBusy(false); }
  }
  useEffect(() => { load(); }, []);
  function exportSummary() {
    if (!summary) return;
    window.location.assign(`/api/stats/report.csv?${buildQuery({ ...appliedFilters, granularity })}`);
  }
  const max = Math.max(...trend.map(row => Math.abs(Number(row.amount))), 1);
  const unitNames: Record<string, string> = { day: '日', week: 'ISO 周', month: '月', year: '年' };
  return <section>
    <form className="filter-strip" onSubmit={e => { e.preventDefault(); load(); }}><div className="filter-strip-title"><span className="filter-symbol">☷</span><div><b>统计范围</b><small>按公司当地开票日期</small></div></div><label>公司<select value={filters.company_id} onChange={e => setFilters({ ...filters, company_id: e.target.value })}><option value="">全部授权公司</option>{companies.map(c => <option key={c.id} value={c.id}>{c.alias || c.legal_name || c.cnpj}</option>)}</select></label><label>开始日期<input type="date" value={filters.date_from} onChange={e => setFilters({ ...filters, date_from: e.target.value })} /></label><label>结束日期<input type="date" value={filters.date_to} onChange={e => setFilters({ ...filters, date_to: e.target.value })} /></label><button className="button primary" disabled={busy}>{busy ? '加载中…' : '应用范围'}</button><button type="button" className="button ghost" onClick={() => { const scope = { date_from: '', date_to: '', company_id: '' }; setFilters(scope); load(scope); }}>全部历史</button></form>
    {error && <div className="alert error">{error}</div>}
    {summary?.all_history_amount !== undefined && <div className="history-summary"><div><b>长期累计销售净额</b><span>{summary.history_start ? `已导入历史从 ${summary.history_start} 开始` : '暂无历史资料'} · 保持当前公司范围</span></div><strong>{money(summary.all_history_amount)}</strong></div>}
    <div className="quick-actions">{can(user, 'import') && <button onClick={() => setPage('imports')}><span>⇧</span><div><b>导入本月 XML</b><small>上传文件并核对导入报告</small></div><i>→</i></button>}<button onClick={() => setPage('invoices')}><span>⌕</span><div><b>查找或复核发票</b><small>查看票头、商品与原始 XML</small></div><i>→</i></button>{can(user, 'export') && <button onClick={() => setPage('exports')}><span>⇩</span><div><b>查看导出任务</b><small>下载已完成的原文 ZIP</small></div><i>→</i></button>}<button onClick={() => setPage('settings')}><span>◷</span><div><b>核对资料覆盖</b><small>确认每月资料是否齐全</small></div><i>→</i></button></div>
    <div className="stat-grid">{summary ? <><StatCard label="销售额" value={money(summary.sales_amount)} hint={`${summary.invoice_count} 张已授权销售票`} tone="green" /><StatCard label="销售退货" value={money(summary.returns_amount)} hint="按退货开票日期扣减" tone="rose" /><StatCard label="销售净额" value={money(summary.net_amount)} hint="销售额 − 销售退货" tone="blue" /><StatCard label="待复核金额" value={money(summary.pending_amount)} hint={`${summary.pending_count} 张需要核对证据或分类`} tone="amber" /></> : <div className="empty span-all">正在加载统计…</div>}</div>
    <div className="dashboard-grid"><div className="panel chart-panel"><div className="panel-title"><div><h2>销售净额趋势</h2><p>按{unitNames[granularity]}汇总 · BRL · 缺失期间不补零</p></div><div className="toolbar-actions"><select className="compact-select" value={granularity} onChange={e => { setGranularity(e.target.value); load(filters, e.target.value); }} aria-label="统计周期"><option value="day">日</option><option value="week">ISO 周</option><option value="month">月</option><option value="year">年</option></select><button className="button ghost small" onClick={exportSummary} disabled={!summary}>下载汇总 CSV</button></div></div>{trend.length ? <div className="chart-bars">{trend.map(row => <div className="chart-col" key={row.period}><div className="chart-value">{money(row.amount)}</div><div className={`chart-bar ${Number(row.amount) < 0 ? 'negative' : ''}`} style={{ height: `${Math.max(5, Math.round(Math.abs(Number(row.amount)) / max * 150))}px` }} /><small>{row.period}</small></div>)}</div> : <div className="empty">暂无可计入销售的数据。请核对导入资料、公司状态和业务分类。</div>}</div>
      <div className="panel coverage-panel"><div className="panel-title"><div><h2>资料与口径提醒</h2><p>先确认覆盖，再对账统计</p></div><span className="info-badge">i</span></div><div className="coverage-note">{summary?.coverage_note || '正在读取资料范围…'}</div><div className="coverage-row"><span>授权销售票</span><strong>{summary?.invoice_count ?? 0} 张</strong></div><div className="coverage-row"><span>待复核</span><strong>{summary?.pending_count ?? 0} 张</strong></div><div className="coverage-row"><span>可见公司</span><strong>{companies.length} 家</strong></div><p className="form-note muted">全部公司统计默认排除集团内部交易；未确认公司、测试票、取消票及不确定业务不会自动计入销售。</p><button className="button link" onClick={() => setPage('settings')}>查看覆盖月份与统计口径 →</button></div>
    </div>
    <div className="panel company-ranking"><div className="panel-title"><div><h2>公司销售净额排名</h2><p>按已应用的日期和公司范围汇总</p></div><button className="button link" onClick={() => setPage('companies')}>查看公司 →</button></div>{ranking.length ? ranking.map((row, index) => <div className="ranking-row" key={row.company_id}><span className="rank">{index + 1}</span><div className="ranking-name"><b>{row.legal_name || row.cnpj}</b><small>{row.cnpj}</small></div><strong>{money(row.amount)}</strong></div>) : <div className="empty">当前范围暂无可排名的销售数据。</div>}</div>
  </section>;
}

function StatCard({ label, value, hint, tone }: { label: string; value: string; hint: string; tone: string }) { return <div className={`stat-card ${tone}`}><div className="stat-card-top"><span>{label}</span><span className="stat-dot" /></div><strong>{value}</strong><small>{hint}</small></div>; }

function Companies({ user }: { user: User }) {
  const [items, setItems] = useState<Company[]>([]);
  const [search, setSearch] = useState('');
  const [selected, setSelected] = useState<Company | null>(null);
  const [editing, setEditing] = useState<Company | null | 'new'>(null);
  const [form, setForm] = useState({ cnpj: '', legal_name: '', alias: '', uf: '', timezone: 'America/Sao_Paulo' });
  const [reason, setReason] = useState('');
  const [confirmChecked, setConfirmChecked] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const load = () => api<Company[]>('/api/companies').then(setItems).catch(e => setError(errorMessage(e)));
  useEffect(() => { load(); }, []);
  const visible = items.filter(c => [c.cnpj, c.legal_name, c.alias].join(' ').toLowerCase().includes(search.toLowerCase()));
  function openEdit(company: Company | null) {
    setError(''); setEditing(company || 'new');
    setForm(company ? { cnpj: company.cnpj, legal_name: company.legal_name, alias: company.alias, uf: company.uf || '', timezone: company.timezone } : { cnpj: '', legal_name: '', alias: '', uf: '', timezone: 'America/Sao_Paulo' });
  }
  async function save(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError('');
    try {
      await api(editing === 'new' ? '/api/companies' : `/api/companies/${(editing as Company).id}`, jsonRequest(editing === 'new' ? 'POST' : 'PATCH', { ...form, confirmed: editing === 'new' ? false : (editing as Company).confirmed }));
      setEditing(null); setSelected(null); setMessage('公司档案已保存。新增公司需核对资料后再单独确认。'); load();
    } catch (e) { setError(errorMessage(e)); } finally { setBusy(false); }
  }
  async function confirmCompany() {
    if (!selected || !reason.trim() || !confirmChecked) return;
    setBusy(true); setError('');
    try {
      await api(`/api/companies/${selected.id}/confirm`, jsonRequest('POST', { reason: reason.trim() }));
      setSelected({ ...selected, confirmed: true }); setReason(''); setConfirmChecked(false);
      setMessage('公司已确认。符合已确认业务规则的票据已重新评估，其他资料仍需复核。'); load();
    } catch (e) { setError(errorMessage(e)); } finally { setBusy(false); }
  }
  return <section>
    <div className="toolbar"><div><div className="search-box"><span>⌕</span><input value={search} onChange={e => setSearch(e.target.value)} placeholder="搜索 CNPJ、公司名" /></div><span className="result-count">{visible.length} 家公司</span></div><div className="toolbar-actions"><button className="button ghost" onClick={load}>刷新</button>{user.role === 'admin' && <button className="button primary" onClick={() => openEdit(null)}>＋ 新建公司</button>}</div></div>
    {message && <div className="alert success-alert">{message}</div>}{error && !editing && !selected && <div className="alert error">{error}</div>}
    <div className="panel table-panel"><table><thead><tr><th>CNPJ</th><th>公司</th><th>票据</th><th>待复核</th><th>UF / 时区</th><th>公司状态</th><th /></tr></thead><tbody>{visible.map(company => <tr key={company.id}><td><code>{company.cnpj}</code></td><td><b>{company.alias || company.legal_name || '未命名公司'}</b><small>{company.legal_name}</small></td><td>{company.invoice_count ?? '—'} 张</td><td>{company.pending_count ?? '—'} 张</td><td>{company.uf || '—'}<small>{company.timezone}</small></td><td><span className={`status ${company.confirmed ? 'success' : 'warning'}`}>{company.confirmed ? '已确认' : '待确认'}</span></td><td><button className="button link" onClick={() => { setSelected(company); setReason(''); setConfirmChecked(false); setError(''); }}>查看详情</button></td></tr>)}</tbody></table>{!visible.length && <div className="empty">暂无公司档案。管理员可以先创建公司并设置 CNPJ。</div>}</div>
    {editing && <Modal title={editing === 'new' ? '新建公司' : '编辑公司'} onClose={() => !busy && setEditing(null)}><form className="modal-form" onSubmit={save}><div className="form-grid"><label>CNPJ<input required value={form.cnpj} onChange={e => setForm({ ...form, cnpj: e.target.value })} placeholder="数字或字母数字 CNPJ" /></label><label>公司简称<input value={form.alias} onChange={e => setForm({ ...form, alias: e.target.value })} placeholder="职员常用名称" /></label><label className="span-2">法定名称 / Razão social<input required value={form.legal_name} onChange={e => setForm({ ...form, legal_name: e.target.value })} /></label><label>州 / UF<input maxLength={2} value={form.uf} onChange={e => setForm({ ...form, uf: e.target.value.toUpperCase() })} placeholder="SP" /></label><label>时区<select value={form.timezone} onChange={e => setForm({ ...form, timezone: e.target.value })}><option>America/Sao_Paulo</option><option>America/Manaus</option><option>America/Belem</option><option>America/Rio_Branco</option></select></label></div><div className="coverage-note">保存档案不会自动确认公司；请在公司详情核对后执行“确认公司”。</div>{error && <div className="alert error">{error}</div>}<div className="modal-actions"><button type="button" className="button ghost" onClick={() => setEditing(null)} disabled={busy}>取消</button><button className="button primary" disabled={busy}>{busy ? '保存中…' : '保存档案'}</button></div></form></Modal>}
    {selected && !editing && <Modal title="公司详情" onClose={() => !busy && setSelected(null)}><div className="detail-list"><div><span>CNPJ</span><code>{selected.cnpj}</code></div><div><span>法定名称</span><b>{selected.legal_name || '—'}</b></div><div><span>公司简称</span><b>{selected.alias || '—'}</b></div><div><span>地区 / 时区</span><b>{selected.uf || '—'} · {selected.timezone}</b></div><div><span>状态</span><span className={`status ${selected.confirmed ? 'success' : 'warning'}`}>{selected.confirmed ? '已确认' : '待确认'}</span></div></div>{user.role === 'admin' && <div className="modal-actions"><button className="button ghost" onClick={() => openEdit(selected)}>编辑公司档案</button></div>}
      {!selected.confirmed && user.role === 'admin' && <div className="review-box"><h3>确认公司归属</h3><p>请核对 CNPJ、名称和时区。确认后，系统会重新评估已有票据；只有授权证据及业务规则满足的票据会参与销售统计。</p><label>确认依据 / 原因<input value={reason} onChange={e => setReason(e.target.value)} placeholder="例如已核对营业登记和本公司财务资料" /></label><label className="check-row"><input type="checkbox" checked={confirmChecked} onChange={e => setConfirmChecked(e.target.checked)} /> 我已核对，该 CNPJ 属于管理范围</label><button className="button primary" disabled={busy || !reason.trim() || !confirmChecked} onClick={confirmCompany}>{busy ? '确认中…' : '确认公司并重新评估票据'}</button></div>}
      {error && <div className="alert error">{error}</div>}
    </Modal>}
  </section>;
}

function Imports({ user }: { user: User }) {
  const [companies, setCompanies] = useState<Company[]>([]);
  const [batches, setBatches] = useState<ImportBatch[]>([]);
  const [files, setFiles] = useState<File[]>([]);
  const [target, setTarget] = useState('');
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  const [error, setError] = useState('');
  const [entries, setEntries] = useState<{ batch: ImportBatch; items: any[] } | null>(null);
  const [entryStatus, setEntryStatus] = useState('');
  const load = () => api<ImportBatch[]>('/api/imports').then(setBatches).catch(e => setError(errorMessage(e)));
  useEffect(() => {
    api<Company[]>('/api/companies').then(list => {
      const permitted = list.filter(c => can(user, 'import', c.id));
      setCompanies(permitted);
      if (user.role !== 'admin' && permitted.length) setTarget(String(permitted[0].id));
    }).catch(e => setError(errorMessage(e)));
    load();
  }, []);
  useEffect(() => { if (!batches.some(b => ['queued', 'processing', 'running'].includes(b.status))) return; const timer = window.setInterval(load, 2500); return () => window.clearInterval(timer); }, [batches]);
  useEffect(() => {
    if (!entries || !['queued', 'processing', 'running'].includes(entries.batch.status)) return;
    const batchId = entries.batch.id;
    const timer = window.setInterval(() => Promise.all([api<ImportBatch>(`/api/imports/${batchId}`), api<any[]>(`/api/imports/${batchId}/entries`)]).then(([batch, items]) => setEntries({ batch, items })).catch(e => setError(errorMessage(e))), 2500);
    return () => window.clearInterval(timer);
  }, [entries?.batch.id, entries?.batch.status]);
  function selectFiles(list: File[]) {
    const invalid = list.filter(file => !/\.(xml|zip)$/i.test(file.name));
    setError(invalid.length ? '仅支持 XML 或 ZIP 文件，请重新选择。' : '');
    if (!invalid.length) setFiles(list);
  }
  async function upload() {
    if (!files.length) return;
    setBusy(true); setMessage(''); setError('');
    const data = new FormData(); files.forEach(file => data.append('files', file)); if (target) data.append('target_company_id', target);
    try {
      const result = await api<any>('/api/imports', { method: 'POST', body: data });
      setMessage(result.status === 'completed' ? `批次 #${result.id} 已完成。请查看批次明细了解新增、重复和异常。` : `批次 #${result.id} 已提交。后台处理结果会自动更新，可离开此页。`);
      setFiles([]); load();
    } catch (e) { setError(errorMessage(e)); } finally { setBusy(false); }
  }
  async function openEntries(batch: ImportBatch) {
    setError('');
    try { const [current, items] = await Promise.all([api<ImportBatch>(`/api/imports/${batch.id}`), api<any[]>(`/api/imports/${batch.id}/entries`)]); setEntries({ batch: current, items }); setEntryStatus(''); } catch (e) { setError(errorMessage(e)); }
  }
  const resultNames: Record<string, string> = { new: '新增', duplicate: '重复', updated: '补充证据', conflict: '冲突', event: '事件', unsupported: '不支持', failed: '失败' };
  return <section>
    <div className="workflow-steps"><span className="current"><i>1</i>选择资料</span><span><i>2</i>后台核验</span><span><i>3</i>查看批次与异常</span><span><i>4</i>确认归属及业务</span></div>
    <div className="import-layout"><div className="panel upload-panel"><div className="upload-symbol">⇧</div><h2>导入 XML 或 ZIP</h2><p>原始文件长期保存；同一发票不会重复计数。导入完成后请查看冲突和待复核资料。</p>
      <label className="file-drop" onDragOver={e => e.preventDefault()} onDrop={e => { e.preventDefault(); selectFiles(Array.from(e.dataTransfer.files)); }}><input type="file" multiple accept=".xml,.zip" disabled={busy} onChange={e => selectFiles(Array.from(e.target.files || []))} /><span>{files.length ? `已选择 ${files.length} 个文件` : '点击选择，或拖入 XML / ZIP'}</span><small>支持多个文件 · 原文不会被改写</small></label>
      {files.length > 0 && <div className="selected-files">{files.map((file, index) => <div key={index}><span>{file.name}</span><small>{(file.size / 1024 / 1024).toFixed(2)} MB</small><button aria-label={`移除 ${file.name}`} disabled={busy} onClick={() => setFiles(files.filter((_, i) => i !== index))}>×</button></div>)}</div>}
      <label>归属公司 / Empresa<select value={target} onChange={e => setTarget(e.target.value)} disabled={busy}>{user.role === 'admin' && <option value="">按票据 CNPJ 识别（不自动确认公司）</option>}{companies.map(c => <option key={c.id} value={c.id}>{c.alias || c.legal_name || c.cnpj}</option>)}</select></label>
      <button className="button primary wide" onClick={upload} disabled={!files.length || busy || (user.role !== 'admin' && !target)}>{busy ? '上传并提交中…' : '开始导入'}</button>{message && <div className="alert success-alert">{message}</div>}{error && <div className="alert error">{error}</div>}
    </div><div className="panel info-panel"><div className="panel-title"><div><h2>导入后要做什么</h2><p>每月资料管理流程</p></div><span className="info-badge">i</span></div><ul className="check-list"><li><b>核对批次计数</b><span>文件成员数、重复数和发票数不同；重复 XML 不会增加销量。</span></li><li><b>查看异常明细</b><span>冲突和解析失败会列出文件名及原因，可下载 CSV 交给负责人复核。</span></li><li><b>确认公司及业务</b><span>新 CNPJ、裸 NFe、采购、调拨、进口等资料会保持待复核。</span></li><li><b>确认覆盖月份</b><span>“已导入”不代表当月已齐全；财务需确认资料覆盖。</span></li></ul></div></div>
    <div className="panel history-panel"><div className="panel-title"><div><h2>导入批次</h2><p>处理中的批次自动刷新</p></div><button className="button ghost small" onClick={load}>刷新批次</button></div><div className="batch-list">{batches.map(batch => <div className="batch-row" key={batch.id}><div><b>批次 #{batch.id}</b><small>{dateTime(batch.created_at)} · 已处理 {batch.counters?.processed ?? batch.counters?.total ?? 0} / {batch.counters?.total || '扫描中'}</small></div><div className="batch-counts">{Object.entries(resultNames).filter(([key]) => (batch.counters?.[key] || 0) > 0).map(([key, label]) => <span key={key} className={`status ${['failed', 'conflict'].includes(key) ? 'danger' : key === 'new' ? 'success' : 'neutral'}`}>{label} {batch.counters[key]}</span>)}</div><span className={`status ${batch.status === 'completed' ? 'success' : batch.status === 'failed' ? 'danger' : 'warning'}`}>{statusLabel[batch.status] || batch.status}</span><div className="row-actions"><button className="button link" onClick={() => openEntries(batch)}>查看明细</button><a className="button link" href={`/api/imports/${batch.id}/report.csv`}>下载报告</a>{['failed', 'partial'].includes(batch.status) && <button className="button link" onClick={() => api(`/api/imports/${batch.id}/retry`, { method: 'POST' }).then(() => { setMessage(`批次 #${batch.id} 已重新排队。`); load(); }).catch(e => setError(errorMessage(e)))}>重试批次</button>}</div></div>)}{!batches.length && <div className="empty">暂无导入批次。选择 XML 或 ZIP 后开始第一次导入。</div>}</div></div>
    {entries && <Modal title={`批次 #${entries.batch.id} · 导入明细`} onClose={() => setEntries(null)}><div className="coverage-note">{statusLabel[entries.batch.status] || entries.batch.status} · 已处理 {entries.batch.counters.processed ?? entries.items.length} / {entries.batch.counters.total || '扫描中'}{entries.batch.report?.net_adjustment !== undefined && <span> · 本批次对历史净额调整 {money(entries.batch.report.net_adjustment)}</span>}</div><div className="toolbar"><label>结果<select value={entryStatus} onChange={e => setEntryStatus(e.target.value)}><option value="">全部文件</option>{Object.entries(resultNames).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label><a className="button ghost small" href={`/api/imports/${entries.batch.id}/report.csv`}>下载 CSV 报告</a></div><div className="entry-list">{entries.items.filter(e => !entryStatus || e.result === entryStatus).map(entry => <div key={entry.id}><span className={`status ${entry.result === 'new' ? 'success' : ['failed', 'conflict'].includes(entry.result) ? 'danger' : 'neutral'}`}>{resultNames[entry.result] || entry.result}</span><code>{entry.member_path}</code><small>{entry.error_code || (entry.invoice_id ? `发票 #${entry.invoice_id}` : entry.event_id ? `事件 #${entry.event_id}` : '')}</small></div>)}</div></Modal>}
  </section>;
}

function buildQuery(filters: Record<string, unknown>) { return new URLSearchParams(Object.entries(filters).filter(([, value]) => value !== undefined && value !== null && value !== '').map(([key, value]) => [key, String(value)])).toString(); }

function InvoiceExplorer({ user, initialLoad = false, onExport }: { user: User; initialLoad?: boolean; onExport: (preset: ExportPreset) => void }) {
  const defaultFilters: InvoiceFilters = { environment: 'production' };
  const [companies, setCompanies] = useState<Company[]>([]);
  const [draft, setDraft] = useState<InvoiceFilters>(defaultFilters);
  const [active, setActive] = useState<InvoiceFilters>(defaultFilters);
  const [items, setItems] = useState<Invoice[]>([]);
  const [total, setTotal] = useState<number | undefined>();
  const [amounts, setAmounts] = useState({ invoice: '0', items: '0' });
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(false);
  const [searched, setSearched] = useState(false);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [allResults, setAllResults] = useState(false);
  const [detail, setDetail] = useState<Invoice | null>(null);
  const [batchOpen, setBatchOpen] = useState(false);
  const [batchMode, setBatchMode] = useState<'selected' | 'all'>('selected');
  const [classification, setClassification] = useState('pending');
  const [classCompany, setClassCompany] = useState('');
  const [reason, setReason] = useState('');
  const [batchBusy, setBatchBusy] = useState(false);
  const [savedFilters, setSavedFilters] = useState<SavedFilter[]>([]);
  const [saveSupported, setSaveSupported] = useState(false);
  const [saveOpen, setSaveOpen] = useState(false);
  const [filterName, setFilterName] = useState('');
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const pageSize = 50;
  useEffect(() => {
    api<Company[]>('/api/companies').then(setCompanies).catch(e => setError(errorMessage(e)));
    api<SavedFilter[]>('/api/saved-filters').then(list => { setSavedFilters(list); setSaveSupported(true); }).catch(() => setSaveSupported(false));
    if (initialLoad) query(defaultFilters, 1, true);
  }, []);
  function validate(filters: InvoiceFilters) {
    if (filters.date_from && filters.date_to && filters.date_from > filters.date_to) throw new Error('开始日期不能晚于结束日期。');
    for (const key of ['min_amount', 'max_amount'] as const) if (filters[key] && !/^\d+(\.\d{1,2})?$/.test(filters[key]!)) throw new Error('金额需填写非负数字，最多两位小数。');
    if (filters.min_amount && filters.max_amount && Number(filters.min_amount) > Number(filters.max_amount)) throw new Error('最小金额不能大于最大金额。');
  }
  async function query(filters: InvoiceFilters, requestedPage: number, resetSelection: boolean) {
    setError(''); setLoading(true);
    try {
      validate(filters);
      const normalized = Object.fromEntries(Object.entries(filters).filter(([, v]) => v !== '' && v !== undefined)) as InvoiceFilters;
      const data = await api<{ items: Invoice[]; total?: number; invoice_amount?: string; matched_items_amount?: string }>(`/api/invoices?${buildQuery({ ...normalized, page: requestedPage, page_size: pageSize })}`);
      setItems(data.items); setTotal(data.total); setPage(requestedPage); setActive(normalized); setSearched(true); setAmounts({ invoice: data.invoice_amount || '0', items: data.matched_items_amount || '0' });
      if (resetSelection) { setSelected(new Set()); setAllResults(false); }
    } catch (e) { setError(errorMessage(e)); } finally { setLoading(false); }
  }
  function update(key: keyof InvoiceFilters, value: string) { setDraft(current => ({ ...current, [key]: value })); }
  function clearSelection() { setSelected(new Set()); setAllResults(false); }
  function selectPage(checked: boolean) { setAllResults(false); setSelected(old => { const next = new Set(old); items.forEach(i => checked ? next.add(i.id) : next.delete(i.id)); return next; }); }
  function selectInvoice(id: number, checked: boolean) { setSelected(old => { const next = new Set(old); checked ? next.add(id) : next.delete(id); return next; }); }
  const selectedCount = allResults ? total : selected.size;
  const hasSelection = allResults || selected.size > 0;
  const canExport = can(user, 'export', active.company_id);
  const canClassify = can(user, 'classify', active.company_id);
  function openBatch() {
    setBatchMode(allResults ? 'all' : 'selected'); setClassCompany(active.company_id || ''); setReason(''); setClassification('pending'); setError(''); setBatchOpen(true);
  }
  async function classify() {
    if (!reason.trim()) { setError('请填写本次复核原因，系统将记录审计。'); return; }
    if (!classCompany) { setError('请选择要调整业务分类的公司，以免混淆购销角色。'); return; }
    if (batchMode === 'selected' && !selected.size) { setError('请先选择发票。'); return; }
    setBatchBusy(true); setError('');
    try {
      const payload = { invoice_ids: batchMode === 'all' ? [] : Array.from(selected), all_results: batchMode === 'all', filters: active, company_id: Number(classCompany), business_class: classification, reason: reason.trim() };
      const result = await api<{ changed: number }>('/api/invoices/classification', jsonRequest('POST', payload));
      setBatchOpen(false); setMessage(`批量复核完成：${result.changed} 条公司票据分类已更新。取消票、冲突票及缺少授权证据的票据仍不自动计入销售。`);
      query(active, page, true);
    } catch (e) { setError(errorMessage(e)); } finally { setBatchBusy(false); }
  }
  async function saveFilter(event: FormEvent) {
    event.preventDefault(); setError('');
    try { validate(draft); await api('/api/saved-filters', jsonRequest('POST', { name: filterName.trim(), filters: draft, filters_json: draft })); setSavedFilters(await api<SavedFilter[]>('/api/saved-filters')); setSaveOpen(false); setFilterName(''); setMessage('筛选方案已保存在服务器。'); } catch (e) { setError(errorMessage(e)); }
  }
  async function openDetail(invoice: Invoice) { try { setDetail(await api<Invoice>(`/api/invoices/${invoice.id}`)); } catch (e) { setError(errorMessage(e)); } }
  return <section>
    <div className="panel filter-card"><div className="filter-card-head"><div><h2>筛选条件</h2><p>金额为整票金额 vNF；SKU、产品名、NCM 和 CFOP 默认要求同一商品行匹配。</p></div><div className="toolbar-actions">{saveSupported && <button className="button ghost small" onClick={() => { setSaveOpen(true); setError(''); }}>保存筛选</button>}<button className="button link" onClick={() => { setDraft(defaultFilters); query(defaultFilters, 1, true); }}>重置筛选</button></div></div>
      {saveSupported && savedFilters.length > 0 && <div className="saved-filter-row"><label>已保存方案<select defaultValue="" onChange={e => { const item = savedFilters.find(f => f.id === Number(e.target.value)); if (item) { const filters = item.filters_json || item.filters || {}; setDraft(filters); query(filters, 1, true); } }}><option value="">选择常用方案</option>{savedFilters.map(f => <option key={f.id} value={f.id}>{f.name}</option>)}</select></label></div>}
      <div className="filter-form">
        <label className="span-2">发票号码或访问键<input placeholder="填写号码或访问键的一部分" value={draft.search || ''} onChange={e => update('search', e.target.value)} /></label>
        <label>公司<select value={draft.company_id || ''} onChange={e => update('company_id', e.target.value)}><option value="">全部授权公司</option>{companies.map(c => <option key={c.id} value={c.id}>{c.alias || c.legal_name || c.cnpj}</option>)}</select></label>
        <label>开票日期起<input type="date" value={draft.date_from || ''} onChange={e => update('date_from', e.target.value)} /></label>
        <label>开票日期止<input type="date" value={draft.date_to || ''} onChange={e => update('date_to', e.target.value)} /></label>
        <label>票据环境<select value={draft.environment || ''} onChange={e => update('environment', e.target.value)}><option value="production">生产票据</option><option value="test">测试票据（不计销售）</option></select></label>
        <label>整票金额最小 / vNF<input inputMode="decimal" placeholder="0.00" value={draft.min_amount || ''} onChange={e => update('min_amount', e.target.value)} /></label>
        <label>整票金额最大 / vNF<input inputMode="decimal" placeholder="不限" value={draft.max_amount || ''} onChange={e => update('max_amount', e.target.value)} /></label>
        <label>SKU / cProd<input placeholder="完整或部分 SKU" value={draft.sku || ''} onChange={e => update('sku', e.target.value)} /><span className="inline-check"><input type="checkbox" checked={draft.sku_exact === 'true'} onChange={e => update('sku_exact', e.target.checked ? 'true' : '')} /> 精确匹配 SKU</span></label>
        <label>产品名 / xProd<input placeholder="支持部分文字匹配" value={draft.product_name || ''} onChange={e => update('product_name', e.target.value)} /></label>
        <label>NCM<input placeholder="例如 85176259" value={draft.ncm || ''} onChange={e => update('ncm', e.target.value)} /></label>
        <label>CFOP<input placeholder="例如 5102" value={draft.cfop || ''} onChange={e => update('cfop', e.target.value)} /></label>
        <label>业务分类<select value={draft.business_class || ''} onChange={e => update('business_class', e.target.value)}><option value="">全部分类</option>{classes.map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label>
        <label>票据状态<select value={draft.status || ''} onChange={e => update('status', e.target.value)}><option value="">全部状态</option><option value="authorized">已授权</option><option value="pending_evidence">待授权证据</option><option value="conflict">冲突待审</option><option value="canceled">已取消</option></select></label>
      </div><div className="filter-actions"><span className="filter-tip">日期按公司当地开票日；上传时间不参与统计</span><button className="button primary" onClick={() => query(draft, 1, true)} disabled={loading}>{loading ? '查询中…' : '应用筛选'}</button></div>
    </div>
    {error && !batchOpen && !saveOpen && <div className="alert error">{error}</div>}{message && <div className="alert success-alert">{message}</div>}
    <div className="panel results-panel"><div className="panel-title"><div><h2>查询结果 <span className="count-badge">{searched ? total ?? items.length : '—'}</span></h2><p>{total !== undefined ? `共 ${total} 张 · 本页 ${items.length} 张` : `本页 ${items.length} 张`} · 导出时保留整张原始 XML</p></div><div className="toolbar-actions">{canClassify && <button className="button ghost" disabled={!hasSelection || loading} onClick={openBatch}>批量复核</button>}{canExport && <button className="button primary" disabled={!hasSelection || loading} onClick={() => onExport({ filters: active, invoice_ids: allResults ? [] : Array.from(selected), all_results: allResults, total: allResults ? total : selected.size })}>导出选中 XML</button>}</div></div>
      {searched && <div className="query-amounts"><span>全部命中发票整票金额 <b>{money(amounts.invoice)}</b></span><span>同商品行命中金额 <b>{money(amounts.items)}</b></span><small>查询金额仅用于核对，不等同于可计入销售净额</small></div>}
      {searched && items.length > 0 && <div className={`selection-bar ${allResults ? 'all' : ''}`}><label className="check-row"><input type="checkbox" checked={!allResults && items.every(i => selected.has(i.id))} disabled={allResults} onChange={e => selectPage(e.target.checked)} />选择当前页 {items.length} 张</label><div>{allResults ? <b>已选择全部查询结果{total !== undefined ? `（${total} 张）` : ''}</b> : <span>已选 {selected.size} 张（可跨页）</span>}<button className="button link" onClick={() => { setAllResults(true); setSelected(new Set()); }}>选择全部查询结果{total !== undefined ? `（${total} 张）` : ''}</button>{hasSelection && <button className="button link" onClick={clearSelection}>取消选择</button>}</div></div>}
      {searched && items.length > 0 ? <InvoiceTable items={items} selected={selected} allResults={allResults} onCheck={selectInvoice} onSelect={openDetail} /> : <div className="empty">{loading ? '正在从数据库加载票据…' : searched ? '没有匹配的发票。请调整筛选范围。' : '设置条件后点击“应用筛选”，查询已持久保存的票据。'}</div>}
      {searched && (items.length > 0 || page > 1) && <div className="pagination"><button className="button ghost small" disabled={page <= 1 || loading} onClick={() => query(active, page - 1, false)}>上一页</button><span>第 {page} 页{total !== undefined ? ` / ${Math.max(1, Math.ceil(total / pageSize))} 页` : ''}</span><button className="button ghost small" disabled={loading || (total !== undefined ? page * pageSize >= total : items.length < pageSize)} onClick={() => query(active, page + 1, false)}>下一页</button></div>}
    </div>
    {detail && <InvoiceDetail user={user} companies={companies} invoice={detail} onClose={() => setDetail(null)} onUpdated={() => { setDetail(null); query(active, page, true); }} />}
    {batchOpen && <Modal title="批量业务复核" onClose={() => !batchBusy && setBatchOpen(false)}><form className="modal-form" onSubmit={e => { e.preventDefault(); classify(); }}><div className="review-box"><label className="check-row"><input type="radio" name="classification-mode" checked={batchMode === 'selected'} disabled={!selected.size} onChange={() => setBatchMode('selected')} /> 仅已勾选的 {selected.size} 张票据</label><label className="check-row"><input type="radio" name="classification-mode" checked={batchMode === 'all'} onChange={() => setBatchMode('all')} /> 全部当前查询结果{total !== undefined ? `（${total} 张）` : ''}，包括其他页面</label></div><div className="form-grid"><label>调整哪家公司的业务分类<select required value={classCompany} onChange={e => setClassCompany(e.target.value)}><option value="">请选择公司</option>{companies.filter(c => can(user, 'classify', c.id)).map(c => <option key={c.id} value={c.id}>{c.alias || c.legal_name || c.cnpj}</option>)}</select></label><label>业务分类<select value={classification} onChange={e => setClassification(e.target.value)}>{classes.map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label><label className="span-2">复核原因（必填）<textarea required value={reason} onChange={e => setReason(e.target.value)} placeholder="说明核对依据与业务性质，原因会写入审计记录" /></label></div><p className="muted form-note">此次操作只修改所选公司的业务角色。票据取消、证据不足或原文冲突会继续限制销售贡献。</p>{error && <div className="alert error">{error}</div>}<div className="modal-actions"><button type="button" className="button ghost" onClick={() => setBatchOpen(false)} disabled={batchBusy}>取消</button><button className="button primary" disabled={batchBusy || !reason.trim() || !classCompany}>{batchBusy ? '保存复核中…' : '确认批量复核'}</button></div></form></Modal>}
    {saveOpen && <Modal title="保存筛选方案" onClose={() => setSaveOpen(false)}><form onSubmit={saveFilter} className="modal-form"><label>方案名称<input required value={filterName} onChange={e => setFilterName(e.target.value)} placeholder="例如本月耳机销售票" /></label><p className="form-note muted">筛选条件保存在服务器，下次登录可直接使用。</p>{error && <div className="alert error">{error}</div>}<div className="modal-actions"><button className="button primary" disabled={!filterName.trim()}>保存方案</button></div></form></Modal>}
  </section>;
}

function InvoiceTable({ items, selected, allResults, onCheck, onSelect }: { items: Invoice[]; selected: Set<number>; allResults: boolean; onCheck: (id: number, checked: boolean) => void; onSelect: (invoice: Invoice) => void }) {
  return <div className="table-scroll"><table><thead><tr><th className="checkbox-cell">选择</th><th>开票日期</th><th>发票号码</th><th>发行方 CNPJ</th><th>整票金额 / vNF</th><th>分类</th><th>状态</th><th /></tr></thead><tbody>{items.map(invoice => <tr key={invoice.id} className={allResults || selected.has(invoice.id) ? 'selected-row' : ''}><td className="checkbox-cell"><input aria-label={`选择发票 ${invoice.number}`} type="checkbox" checked={allResults || selected.has(invoice.id)} disabled={allResults} onChange={e => onCheck(invoice.id, e.target.checked)} /></td><td>{dateOnly(invoice.issue_local_date)}</td><td><b>NF {invoice.number || '—'}</b><small>Série {invoice.series || '—'} · {invoice.items.length} 行商品</small></td><td><code>{invoice.issuer_cnpj || '—'}</code></td><td className="amount">{money(invoice.v_nf)}</td><td><span className="tag">{businessLabel[invoice.roles[0]?.business_class] || '待确认'}</span></td><td><span className={`status ${invoice.conflict ? 'danger' : invoice.fiscal_status === 'authorized' ? 'success' : 'warning'}`}>{invoice.conflict ? '冲突待审' : statusLabel[invoice.fiscal_status] || invoice.fiscal_status}</span></td><td><button className="button link" onClick={() => onSelect(invoice)}>查看</button></td></tr>)}</tbody></table></div>;
}

function InvoiceDetail({ user, companies, invoice, onClose, onUpdated }: { user: User; companies: Company[]; invoice: Invoice; onClose: () => void; onUpdated: () => void }) {
  const allowedRoles = invoice.roles.filter(role => can(user, 'classify', role.company_id));
  const [companyId, setCompanyId] = useState(String(allowedRoles[0]?.company_id || ''));
  const [classification, setClassification] = useState(allowedRoles[0]?.business_class || 'pending');
  const [reason, setReason] = useState('');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [conflictReason, setConflictReason] = useState('');
  const [conflictChecked, setConflictChecked] = useState(false);
  const detailInvoice = invoice as Invoice & { review_reason?: string; versions?: { id: number; document_kind: string; sha256: string; is_primary: boolean; download: string }[]; events?: { event_type: string; accepted: boolean; receipt_status: string; protocol: string }[] };
  async function save() {
    if (!reason.trim() || !companyId) return; setSaving(true); setError('');
    try { await api(`/api/invoices/${invoice.id}/classification`, jsonRequest('PATCH', { business_class: classification, company_id: Number(companyId), reason: reason.trim() })); onUpdated(); } catch (e) { setError(errorMessage(e)); } finally { setSaving(false); }
  }
  async function retainPrimary() {
    if (!conflictReason.trim() || !conflictChecked) return; setSaving(true); setError('');
    try { await api(`/api/invoices/${invoice.id}/resolve-conflict`, jsonRequest('POST', { reason: conflictReason.trim() })); onUpdated(); } catch (e) { setError(errorMessage(e)); } finally { setSaving(false); }
  }
  const companyName = (id: number) => { const company = companies.find(c => c.id === id); return company?.alias || company?.legal_name || `公司 #${id}`; };
  return <Modal title={`发票详情 · NF ${invoice.number || invoice.id}`} onClose={onClose}><div className="invoice-overview"><div className="span-2"><span>访问键 / Chave de acesso</span><code>{invoice.access_key}</code></div><div><span>开票日期</span><b>{dateOnly(invoice.issue_local_date)}</b></div><div><span>整票金额 / vNF</span><strong>{money(invoice.v_nf)}</strong></div><div><span>票据状态</span><span className={`status ${invoice.fiscal_status === 'authorized' && !invoice.conflict ? 'success' : 'warning'}`}>{statusLabel[invoice.fiscal_status] || invoice.fiscal_status}</span></div><div><span>环境</span><b>{invoice.environment === 'production' ? '生产' : '测试'}</b></div></div>
    {detailInvoice.review_reason && <div className="coverage-note">复核说明：{detailInvoice.review_reason}</div>}
    <div className="detail-section"><h3>公司角色与销售贡献</h3>{invoice.roles.map(role => <div className="company-role-row" key={role.company_id}><b>{companyName(role.company_id)}</b><span>{role.role === 'issuer' ? '发行方' : '接收方'} · {businessLabel[role.business_class] || role.business_class}</span><span className={`status ${role.included ? 'success' : 'neutral'}`}>{role.included ? '参与销售贡献' : '不计入销售'}</span></div>)}</div>
    <div className="detail-section"><h3>商品明细</h3><div className="line-items">{invoice.items.map(item => <div key={item.line_no}><div><code>{item.c_prod || '—'}</code><small>NCM {item.ncm || '—'} · CFOP {item.cfop || '—'}</small></div><span>{item.x_prod || '—'}<small>数量 {item.q_com}</small></span><b>{money(item.v_prod)}</b></div>)}</div></div>
    {detailInvoice.events && detailInvoice.events.length > 0 && <div className="detail-section"><h3>票据事件</h3>{detailInvoice.events.map((event, index) => <div className="company-role-row" key={index}><code>{event.event_type}</code><span>{event.accepted ? '已接受' : '待确认'} · cStat {event.receipt_status}</span><code>{event.protocol}</code></div>)}</div>}
    {detailInvoice.versions && detailInvoice.versions.length > 0 && <div className="detail-section"><h3>不可变原文版本</h3>{detailInvoice.versions.map(version => <div className="version-row" key={version.id}><div><b>版本 #{version.id} · {version.document_kind}</b><small>SHA-256 {version.sha256}</small></div>{version.is_primary && <span className="tag">默认原文</span>}{can(user, 'export') && <a className="button link" href={version.download}>下载 XML</a>}</div>)}</div>}
    {invoice.conflict && user.role === 'admin' && <div className="detail-section review-box"><h3>冲突复核：保留现有主版本</h3><p>请先对比原文版本及外部凭据。此操作仅保留现有主版本、解除冲突标记；所有冲突文件仍保留，已接受取消事件仍有效。</p><label>保留现有主版本的依据<textarea value={conflictReason} onChange={e => setConflictReason(e.target.value)} placeholder="说明票据核对依据，不可空白" /></label><label className="check-row"><input type="checkbox" checked={conflictChecked} onChange={e => setConflictChecked(e.target.checked)} /> 已核对原文和业务凭据，确认保留当前主版本</label><button className="button ghost" disabled={saving || !conflictReason.trim() || !conflictChecked} onClick={retainPrimary}>保留主版本并解除冲突</button></div>}
    {allowedRoles.length > 0 && <div className="detail-section review-box"><h3>业务分类复核</h3><div className="form-grid"><label>公司角色<select value={companyId} onChange={e => { setCompanyId(e.target.value); setClassification(allowedRoles.find(r => r.company_id === Number(e.target.value))?.business_class || 'pending'); }}>{allowedRoles.map(role => <option key={role.company_id} value={role.company_id}>{companyName(role.company_id)}</option>)}</select></label><label>业务分类<select value={classification} onChange={e => setClassification(e.target.value)}>{classes.map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label><label className="span-2">复核原因<textarea required value={reason} onChange={e => setReason(e.target.value)} placeholder="说明核对依据，不可空白" /></label></div><div className="modal-actions"><button className="button primary" onClick={save} disabled={saving || !reason.trim()}>{saving ? '保存中…' : '保存分类与原因'}</button></div></div>}
    {error && <div className="alert error">{error}</div>}
  </Modal>;
}

function Exports({ user, preset }: { user: User; preset: ExportPreset }) {
  const [jobs, setJobs] = useState<ExportJob[]>([]);
  const [includeEvents, setIncludeEvents] = useState(false);
  const [allVersions, setAllVersions] = useState(false);
  const [acknowledged, setAcknowledged] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const [details, setDetails] = useState<any | null>(null);
  const load = () => api<ExportJob[]>('/api/exports').then(setJobs).catch(e => setError(errorMessage(e)));
  useEffect(() => { load(); setAcknowledged(false); }, [preset]);
  useEffect(() => { if (!jobs.some(job => ['queued', 'processing', 'running'].includes(job.status))) return; const timer = window.setInterval(load, 2500); return () => window.clearInterval(timer); }, [jobs]);
  async function create() {
    setBusy(true); setError(''); setMessage('');
    try {
      const result = await api<{ id: number }>('/api/exports', jsonRequest('POST', { filters: preset.filters, invoice_ids: preset.all_results ? [] : preset.invoice_ids || [], include_events: includeEvents, all_versions: allVersions }));
      setMessage(`任务 #${result.id} 已提交。结果与原文版本在创建时冻结，后台完成后可下载 ZIP。`); setAcknowledged(false); load();
    } catch (e) { setError(errorMessage(e)); } finally { setBusy(false); }
  }
  const labels: Record<string, string> = { company_id: '公司 ID', date_from: '开票日期起', date_to: '开票日期止', min_amount: '整票金额最小', max_amount: '整票金额最大', sku: 'SKU', sku_exact: '精确 SKU', product_name: '产品名', ncm: 'NCM', cfop: 'CFOP', status: '状态', business_class: '分类', environment: '环境' };
  const conditions = Object.entries(preset.filters).filter(([, value]) => value !== '');
  return <section>
    <div className="panel export-config"><div className="export-hero"><div className="export-hero-icon">⇩</div><div><h2>确认导出范围</h2><p>按不可变原始 XML 生成 ZIP。导出后台处理，关闭页面不会取消任务。</p><strong className="export-scope">{preset.all_results ? `全部当前查询结果${preset.total !== undefined ? ` · ${preset.total} 张` : ''}` : `手动勾选 · ${preset.invoice_ids?.length || 0} 张发票`}</strong></div></div>
      <div className="export-query-summary">{conditions.length ? conditions.map(([key, value]) => <span key={key}><b>{labels[key] || key}</b> {String(value)}</span>) : <div className="coverage-note">没有额外筛选条件，将按权限范围选择全部生产票据。下载文件不改变统计数据。</div>}</div>
      <div className="export-options"><label className="check-row"><input type="checkbox" checked={includeEvents} onChange={e => setIncludeEvents(e.target.checked)} /> 包含对应事件 XML（例如取消事件）</label><label className="check-row"><input type="checkbox" checked={allVersions} onChange={e => setAllVersions(e.target.checked)} /> 包含全部原文版本（默认只导出主版本）</label></div>
      <div className="coverage-note">ZIP 内附 manifest，记录访问键、文件版本与 SHA-256。筛选商品只决定是否命中发票，导出始终保留整张 XML。</div>
      <div className="export-submit"><label className="check-row"><input type="checkbox" checked={acknowledged} onChange={e => setAcknowledged(e.target.checked)} /> 我已核对导出范围与选项</label><button className="button primary" onClick={create} disabled={busy || !acknowledged || !can(user, 'export', preset.filters.company_id) || (!preset.all_results && !preset.invoice_ids?.length)}>{busy ? '创建任务中…' : '创建 XML ZIP 导出任务'}</button></div>
    </div>
    {message && <div className="alert success-alert">{message}</div>}{error && <div className="alert error">{error}</div>}
    <div className="panel history-panel"><div className="panel-title"><div><h2>导出历史</h2><p>处理中任务自动刷新；失败任务显示原因</p></div><button className="button ghost small" onClick={load}>刷新</button></div><div className="export-list">{jobs.map(job => <div className="export-row" key={job.id}><div className="export-id"><b>任务 #{job.id}</b><small>{dateTime(job.created_at)}</small></div><span className={`status ${job.status === 'completed' ? 'success' : job.status === 'failed' ? 'danger' : 'warning'}`}>{statusLabel[job.status] || job.status}</span><span>{job.file_count ?? '—'} 个文件</span><span>{job.snapshot?.invoice_count ?? '—'} 张票据</span><div className="row-actions"><button className="button link" onClick={() => api(`/api/exports/${job.id}`).then(setDetails).catch(e => setError(errorMessage(e)))}>任务详情</button>{job.status === 'completed' && <a className="button link" href={`/api/exports/${job.id}/download`}>下载 ZIP</a>}{job.status === 'failed' && <button className="button link" onClick={() => api(`/api/exports/${job.id}/retry`, { method: 'POST' }).then(() => { setMessage(`任务 #${job.id} 已重新排队。`); load(); }).catch(e => setError(errorMessage(e)))}>重试任务</button>}</div></div>)}{!jobs.length && <div className="empty">暂无导出任务。可先在发票中心筛选并勾选票据，再导出选中结果。</div>}</div></div>
    {details && <Modal title={`导出任务 #${details.id}`} onClose={() => setDetails(null)}><div className="detail-list"><div><span>状态</span><b>{statusLabel[details.status] || details.status}</b></div><div><span>原文文件数</span><b>{details.file_count ?? '—'}</b></div><div><span>冻结发票数</span><b>{details.snapshot?.invoice_count ?? '—'}</b></div><div><span>快照时间</span><b>{dateTime(details.snapshot?.created_at)}</b></div></div>{(details.error || details.snapshot?.error) && <div className="alert error">{details.error || details.snapshot.error}</div>}{details.download && <div className="modal-actions"><a className="button primary" href={details.download}>下载 ZIP</a></div>}</Modal>}
  </section>;
}

type SettingsTab = 'account' | 'coverage' | 'users' | 'rules' | 'audit' | 'backups';
function Settings({ user }: { user: User }) {
  const [tab, setTab] = useState<SettingsTab>('account');
  const [companies, setCompanies] = useState<Company[]>([]);
  useEffect(() => { api<Company[]>('/api/companies').then(setCompanies).catch(() => undefined); }, []);
  const tabs: { key: SettingsTab; label: string; admin?: boolean }[] = [{ key: 'account', label: '账号与统计口径' }, { key: 'coverage', label: '覆盖月份' }, { key: 'users', label: '用户与公司权限', admin: true }, { key: 'rules', label: '业务分类规则', admin: true }, { key: 'audit', label: '审计记录', admin: true }, { key: 'backups', label: '备份与恢复验证', admin: true }];
  return <section><div className="settings-tabs">{tabs.filter(item => !item.admin || user.role === 'admin').map(item => <button key={item.key} className={tab === item.key ? 'active' : ''} onClick={() => setTab(item.key)}>{item.label}</button>)}</div>
    {tab === 'account' && <AccountPanel user={user} companies={companies} />}
    {tab === 'coverage' && <CoveragePanel user={user} companies={companies} />}
    {tab === 'users' && user.role === 'admin' && <UsersPanel companies={companies} />}
    {tab === 'rules' && user.role === 'admin' && <RulesPanel companies={companies} />}
    {tab === 'audit' && user.role === 'admin' && <AuditPanel />}
    {tab === 'backups' && user.role === 'admin' && <BackupsPanel />}
  </section>;
}

function AccountPanel({ user, companies }: { user: User; companies: Company[] }) {
  const [oldPassword, setOldPassword] = useState(''); const [newPassword, setNewPassword] = useState(''); const [repeat, setRepeat] = useState('');
  const [busy, setBusy] = useState(false); const [error, setError] = useState(''); const [message, setMessage] = useState('');
  async function changePassword(event: FormEvent) {
    event.preventDefault(); setError(''); setMessage('');
    if (newPassword !== repeat) { setError('两次填写的新密码不一致。'); return; }
    setBusy(true);
    try { await api('/api/me/password', jsonRequest('PATCH', { old_password: oldPassword, new_password: newPassword })); setMessage('密码已更新。'); setOldPassword(''); setNewPassword(''); setRepeat(''); } catch (e) { setError(errorMessage(e)); } finally { setBusy(false); }
  }
  const permissionLabels: Record<string, string> = { view: '查看', import: '导入', export: '导出', classify: '分类复核', manage: '管理' };
  return <div className="account-layout"><div className="panel settings-card"><div className="panel-title"><div><h2>我的账号</h2><p>权限由服务端验证</p></div><span className="tag">{user.role === 'admin' ? '管理员' : user.role === 'finance' ? '财务' : user.role === 'readonly' ? '只读' : '职员'}</span></div><div className="detail-list"><div><span>登录名</span><b>{user.login}</b></div><div><span>姓名</span><b>{user.display_name || '—'}</b></div></div>{user.role === 'admin' ? <p>管理员可查看全部公司，并管理用户、公司、分类规则和备份。</p> : <div className="scope-summary">{user.scopes?.map(scope => <div key={scope.company_id}><b>{companies.find(c => c.id === scope.company_id)?.alias || companies.find(c => c.id === scope.company_id)?.legal_name || `公司 #${scope.company_id}`}</b><small>{scope.permissions.map(p => permissionLabels[p] || p).join('、')}</small></div>)}{!user.scopes?.length && <div className="coverage-note">尚未授权公司范围，请联系管理员。</div>}</div>}
      <form className="password-form" onSubmit={changePassword}><h3>修改密码</h3><label>当前密码<input required type="password" autoComplete="current-password" value={oldPassword} onChange={e => setOldPassword(e.target.value)} /></label><label>新密码（至少 10 位）<input required minLength={10} type="password" autoComplete="new-password" value={newPassword} onChange={e => setNewPassword(e.target.value)} /></label><label>再次输入新密码<input required type="password" autoComplete="new-password" value={repeat} onChange={e => setRepeat(e.target.value)} /></label>{error && <div className="alert error">{error}</div>}{message && <div className="alert success-alert">{message}</div>}<button className="button primary" disabled={busy}>{busy ? '保存中…' : '更新密码'}</button></form>
    </div><div className="panel settings-card"><h2>统计口径说明</h2><ol className="policy-list"><li><b>开票日期</b><span>以 XML 开票时间和公司当地日期划分日、ISO 周、月与年，不使用上传时间。</span></li><li><b>销售贡献</b><span>公司已确认、生产环境、授权证据有效、未被接受取消且业务分类为销售或销售退货时，才按规则参与。</span></li><li><b>票头金额</b><span>销售额以整票金额 vNF 为准，多商品 JOIN 不会重复计数。商品命中金额仅为查询辅助。</span></li><li><b>退货与内部交易</b><span>销售退货按退货票日期扣减；采购退货另行分类。集团汇总默认排除内部交易。</span></li><li><b>资料覆盖</b><span>未导入或未确认齐全的月份，不能当作零销售。每月由财务确认覆盖情况。</span></li><li><b>原文与去重</b><span>原始 XML 不可变，SHA-256 校验；同一票身份重复不累计，冲突资料隔离并保留审计。</span></li></ol><div className="coverage-note">规则依据需由负责会计确认。系统不执行开票、自动报税或 SEFAZ 联机。</div></div></div>;
}

type Coverage = { id: number; company_id: number; month: string; coverage_state: string; notes: string; confirmed_at?: string };
function CoveragePanel({ user, companies }: { user: User; companies: Company[] }) {
  const [rows, setRows] = useState<Coverage[]>([]); const [company, setCompany] = useState(''); const [editing, setEditing] = useState<{ company_id: string; month: string; coverage_state: string; notes: string } | null>(null);
  const [busy, setBusy] = useState(false); const [error, setError] = useState(''); const [message, setMessage] = useState('');
  const labels: Record<string, string> = { not_imported: '未导入', partial: '部分资料 / 待确认', complete: '已确认齐全' };
  const maintain = can(user, 'classify');
  const load = (id = company) => api<Coverage[]>(`/api/coverage${id ? `?company_id=${id}` : ''}`).then(setRows).catch(e => setError(errorMessage(e)));
  useEffect(() => { load(); }, []);
  async function save(event: FormEvent) {
    event.preventDefault(); if (!editing) return; setBusy(true); setError('');
    try { await api(`/api/companies/${editing.company_id}/coverage/${editing.month}`, jsonRequest('PATCH', { coverage_state: editing.coverage_state, notes: editing.notes.trim() })); setEditing(null); setMessage('覆盖月份已更新并记录确认人和时间。'); load(); } catch (e) { setError(errorMessage(e)); } finally { setBusy(false); }
  }
  return <div className="panel history-panel"><div className="panel-title"><div><h2>每月资料覆盖</h2><p>覆盖状态与销售金额分开管理；没有资料不是零销售。</p></div><div className="toolbar-actions"><button className="button ghost small" onClick={() => load()}>刷新</button>{maintain && <button className="button primary" onClick={() => { setEditing({ company_id: company, month: '', coverage_state: 'partial', notes: '' }); setError(''); }}>＋ 确认月份</button>}</div></div><div className="settings-filter"><label>公司<select value={company} onChange={e => { setCompany(e.target.value); load(e.target.value); }}><option value="">全部授权公司</option>{companies.map(c => <option key={c.id} value={c.id}>{c.alias || c.legal_name || c.cnpj}</option>)}</select></label></div>
    {error && !editing && <div className="alert error">{error}</div>}{message && <div className="alert success-alert">{message}</div>}
    <div className="table-scroll"><table><thead><tr><th>月份</th><th>公司</th><th>资料状态</th><th>确认时间</th><th>备注</th><th /></tr></thead><tbody>{rows.map(row => <tr key={row.id}><td>{row.month}</td><td>{companies.find(c => c.id === row.company_id)?.alias || companies.find(c => c.id === row.company_id)?.legal_name || `公司 #${row.company_id}`}</td><td><span className={`status ${row.coverage_state === 'complete' ? 'success' : 'warning'}`}>{labels[row.coverage_state] || row.coverage_state}</span></td><td>{dateTime(row.confirmed_at)}</td><td className="wrap-cell">{row.notes || '—'}</td><td>{maintain && can(user, 'classify', row.company_id) && <button className="button link" onClick={() => { setEditing({ company_id: String(row.company_id), month: row.month, coverage_state: row.coverage_state, notes: row.notes || '' }); setError(''); }}>更新状态</button>}</td></tr>)}</tbody></table>{!rows.length && <div className="empty">暂无覆盖月份记录。缺失期间不得视为零销售。</div>}</div>
    {editing && <Modal title="确认资料覆盖月份" onClose={() => !busy && setEditing(null)}><form className="modal-form" onSubmit={save}><div className="form-grid"><label>公司<select required value={editing.company_id} onChange={e => setEditing({ ...editing, company_id: e.target.value })}><option value="">请选择公司</option>{companies.filter(c => can(user, 'classify', c.id)).map(c => <option key={c.id} value={c.id}>{c.alias || c.legal_name || c.cnpj}</option>)}</select></label><label>月份<input required type="month" value={editing.month} onChange={e => setEditing({ ...editing, month: e.target.value })} /></label><label>资料状态<select value={editing.coverage_state} onChange={e => setEditing({ ...editing, coverage_state: e.target.value })}>{Object.entries(labels).map(([key, value]) => <option key={key} value={key}>{value}</option>)}</select></label><label className="span-2">核对备注<textarea required value={editing.notes} onChange={e => setEditing({ ...editing, notes: e.target.value })} placeholder="注明资料来源、缺失情况或齐全核对依据" /></label></div><div className="coverage-note">选择“已确认齐全”前，请核对公司当月所有销售、退货及取消事件资料。</div>{error && <div className="alert error">{error}</div>}<div className="modal-actions"><button className="button primary" disabled={busy || !editing.company_id || !editing.month || !editing.notes.trim()}>{busy ? '保存中…' : '保存覆盖状态'}</button></div></form></Modal>}
  </div>;
}

function UsersPanel({ companies }: { companies: Company[] }) {
  const [users, setUsers] = useState<User[]>([]); const [editing, setEditing] = useState<User | 'new' | null>(null);
  const [form, setForm] = useState({ login: '', display_name: '', role: 'readonly', password: '', active: true, scopes: [] as Scope[] });
  const [busy, setBusy] = useState(false); const [error, setError] = useState(''); const [message, setMessage] = useState('');
  const load = () => api<User[]>('/api/users').then(setUsers).catch(e => setError(errorMessage(e))); useEffect(() => { load(); }, []);
  function open(item: User | null) { setEditing(item || 'new'); setError(''); setForm(item ? { login: item.login, display_name: item.display_name, role: item.role, password: '', active: item.active !== false, scopes: item.scopes?.map(s => ({ ...s, permissions: [...s.permissions] })) || [] } : { login: '', display_name: '', role: 'readonly', password: '', active: true, scopes: [] }); }
  function toggleCompany(companyId: number, checked: boolean) { setForm(old => ({ ...old, scopes: checked ? [...old.scopes.filter(s => s.company_id !== companyId), { company_id: companyId, permissions: ['view'] }] : old.scopes.filter(s => s.company_id !== companyId) })); }
  function togglePermission(companyId: number, permission: string, checked: boolean) { setForm(old => ({ ...old, scopes: old.scopes.map(s => s.company_id === companyId ? { ...s, permissions: checked ? Array.from(new Set([...s.permissions, permission])) : s.permissions.filter(p => p !== permission) } : s) })); }
  async function save(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError('');
    try { const payload: any = { ...form }; if (!form.password) delete payload.password; await api(editing === 'new' ? '/api/users' : `/api/users/${(editing as User).id}`, jsonRequest(editing === 'new' ? 'POST' : 'PATCH', payload)); setEditing(null); setMessage('账号与公司权限已保存。'); load(); } catch (e) { setError(errorMessage(e)); } finally { setBusy(false); }
  }
  return <div className="panel history-panel"><div className="panel-title"><div><h2>用户与公司范围</h2><p>新账号默认只读；只授权实际需要的公司和操作。</p></div><div className="toolbar-actions"><button className="button ghost small" onClick={load}>刷新</button><button className="button primary" onClick={() => open(null)}>＋ 新建用户</button></div></div>{error && !editing && <div className="alert error">{error}</div>}{message && <div className="alert success-alert">{message}</div>}<div className="table-scroll"><table><thead><tr><th>姓名 / 账号</th><th>角色</th><th>公司范围</th><th>状态</th><th /></tr></thead><tbody>{users.map(item => <tr key={item.id}><td><b>{item.display_name || item.login}</b><small>{item.login}</small></td><td>{item.role === 'admin' ? '管理员' : item.role === 'finance' ? '财务' : item.role === 'readonly' ? '只读' : item.role}</td><td>{item.role === 'admin' ? '全部公司' : `${item.scopes?.length || 0} 家公司`}</td><td><span className={`status ${item.active !== false ? 'success' : 'neutral'}`}>{item.active !== false ? '启用' : '已停用'}</span></td><td><button className="button link" onClick={() => open(item)}>编辑权限</button></td></tr>)}</tbody></table></div>
    {editing && <Modal title={editing === 'new' ? '新建用户' : '编辑账号与公司权限'} onClose={() => !busy && setEditing(null)}><form className="modal-form" onSubmit={save}><div className="form-grid"><label>登录名<input required disabled={editing !== 'new'} value={form.login} onChange={e => setForm({ ...form, login: e.target.value })} /></label><label>姓名<input required value={form.display_name} onChange={e => setForm({ ...form, display_name: e.target.value })} /></label><label>角色<select value={form.role} onChange={e => setForm({ ...form, role: e.target.value })}><option value="readonly">只读职员</option><option value="finance">财务职员</option><option value="accountant">会计</option><option value="admin">管理员（全部公司）</option></select></label><label>{editing === 'new' ? '初始密码（至少 10 位）' : '重置密码（留空保留原密码）'}<input type="password" minLength={10} required={editing === 'new'} value={form.password} onChange={e => setForm({ ...form, password: e.target.value })} autoComplete="new-password" /></label></div><label className="check-row"><input type="checkbox" checked={form.active} onChange={e => setForm({ ...form, active: e.target.checked })} /> 账号启用</label>{form.role !== 'admin' && <div className="permission-editor"><h3>公司范围及操作权限</h3>{companies.map(company => { const scope = form.scopes.find(s => s.company_id === company.id); return <div key={company.id}><label className="check-row"><input type="checkbox" checked={Boolean(scope)} onChange={e => toggleCompany(company.id, e.target.checked)} /> {company.alias || company.legal_name || company.cnpj}</label><div className="permission-choices">{[['view', '查看'], ['import', '导入'], ['classify', '分类复核'], ['export', '导出']].map(([key, label]) => <label key={key} className="check-row"><input type="checkbox" disabled={!scope || key === 'view'} checked={Boolean(scope?.permissions.includes(key))} onChange={e => togglePermission(company.id, key, e.target.checked)} />{label}</label>)}</div></div>; })}{!companies.length && <div className="empty">请先建立公司档案。</div>}</div>}{form.role === 'admin' && <div className="coverage-note">管理员拥有全部公司与系统管理权限，请谨慎授权。</div>}{error && <div className="alert error">{error}</div>}<div className="modal-actions"><button type="button" className="button ghost" onClick={() => setEditing(null)} disabled={busy}>取消</button><button className="button primary" disabled={busy}>{busy ? '保存中…' : '保存用户与权限'}</button></div></form></Modal>}
  </div>;
}

type Rule = { id: number; version: string; company_id: number | null; conditions_json: Record<string, any>; result_class: string; active: boolean };
function RulesPanel({ companies }: { companies: Company[] }) {
  const [rules, setRules] = useState<Rule[]>([]); const [editing, setEditing] = useState<Rule | 'new' | null>(null);
  const [form, setForm] = useState({ version: '1.0', company_id: '', cfops: '', tp_nf: '1', fin_nfe: '1', role: 'issuer', model: '55', result_class: 'pending', active: true });
  const [ack, setAck] = useState(false); const [busy, setBusy] = useState(false); const [error, setError] = useState(''); const [message, setMessage] = useState('');
  const load = () => api<Rule[]>('/api/rules').then(setRules).catch(e => setError(errorMessage(e))); useEffect(() => { load(); }, []);
  const value = (input: unknown, fallback: string) => Array.isArray(input) ? input.join(',') : input === undefined ? fallback : String(input);
  function open(item: Rule | null) { setEditing(item || 'new'); setError(''); setAck(false); setForm(item ? { version: item.version, company_id: item.company_id ? String(item.company_id) : '', cfops: (item.conditions_json.cfops || []).join(', '), tp_nf: value(item.conditions_json.tp_nf, '1'), fin_nfe: value(item.conditions_json.fin_nfe, '1'), role: value(item.conditions_json.role, 'issuer'), model: value(item.conditions_json.model, '55'), result_class: item.result_class, active: item.active } : { version: '1.0', company_id: '', cfops: '', tp_nf: '1', fin_nfe: '1', role: 'issuer', model: '55', result_class: 'pending', active: true }); }
  async function save(event: FormEvent) {
    event.preventDefault(); setError('');
    const cfops = Array.from(new Set(form.cfops.split(/[,，;；\s]+/).filter(Boolean)));
    if (!cfops.length || cfops.some(c => !/^\d{4}$/.test(c))) { setError('CFOP 请填写四位代码，以逗号或空格分隔。'); return; }
    const parse = (v: string) => v.includes(',') ? v.split(',').filter(Boolean) : v;
    const conditions = { cfops, tp_nf: parse(form.tp_nf), fin_nfe: parse(form.fin_nfe), role: form.result_class === 'sale' ? 'issuer' : parse(form.role), ...(form.model ? { model: form.model } : {}) };
    setBusy(true);
    try { await api(editing === 'new' ? '/api/rules' : `/api/rules/${(editing as Rule).id}`, jsonRequest(editing === 'new' ? 'POST' : 'PATCH', { version: form.version, company_id: form.company_id ? Number(form.company_id) : null, conditions_json: conditions, result_class: form.result_class, active: form.active })); setEditing(null); setMessage('规则已保存。真实税务分类需由负责会计确认，系统不会根据单一方向字段猜测销售。'); load(); } catch (e) { setError(errorMessage(e)); } finally { setBusy(false); }
  }
  return <div className="panel history-panel"><div className="panel-title"><div><h2>业务分类规则</h2><p>规则必须同时限定全部 CFOP、发票方向、目的和公司角色。</p></div><div className="toolbar-actions"><button className="button ghost small" onClick={load}>刷新</button><button className="button primary" onClick={() => open(null)}>＋ 新建规则</button></div></div><div className="coverage-note">此页面仅供已掌握分类依据的管理员使用。规则不会代替授权/取消事件判断，不支持把所有出库票直接算销售。</div>{error && !editing && <div className="alert error">{error}</div>}{message && <div className="alert success-alert">{message}</div>}<div className="table-scroll"><table><thead><tr><th>版本</th><th>公司</th><th>CFOP 范围</th><th>公司角色</th><th>分类结果</th><th>状态</th><th /></tr></thead><tbody>{rules.map(rule => <tr key={rule.id}><td>{rule.version}</td><td>{rule.company_id ? companies.find(c => c.id === rule.company_id)?.alias || companies.find(c => c.id === rule.company_id)?.legal_name || `#${rule.company_id}` : '全部公司'}</td><td className="wrap-cell"><code>{(rule.conditions_json.cfops || []).join(', ') || '未完整配置'}</code></td><td>{value(rule.conditions_json.role, '—') === 'issuer' ? '发行方' : value(rule.conditions_json.role, '—') === 'recipient' ? '接收方' : value(rule.conditions_json.role, '—')}</td><td>{businessLabel[rule.result_class] || rule.result_class}</td><td><span className={`status ${rule.active ? 'success' : 'neutral'}`}>{rule.active ? '启用' : '停用'}</span></td><td><button className="button link" onClick={() => open(rule)}>编辑规则</button></td></tr>)}</tbody></table>{!rules.length && <div className="empty">暂无自定义规则。系统保留保守内置规则，不确定业务进入待确认。</div>}</div>
    {editing && <Modal title={editing === 'new' ? '新建分类规则' : '编辑分类规则'} onClose={() => !busy && setEditing(null)}><form className="modal-form" onSubmit={save}><div className="form-grid"><label>规则版本<input required value={form.version} onChange={e => setForm({ ...form, version: e.target.value })} /></label><label>适用公司<select value={form.company_id} onChange={e => setForm({ ...form, company_id: e.target.value })}><option value="">全部公司</option>{companies.map(c => <option key={c.id} value={c.id}>{c.alias || c.legal_name || c.cnpj}</option>)}</select></label><label className="span-2">允许的全部 CFOP（四位，以逗号分隔）<input required value={form.cfops} onChange={e => setForm({ ...form, cfops: e.target.value })} placeholder="例如 5101, 5102, 6101, 6102" /><span>一张发票的所有商品行 CFOP 均需属于此范围。</span></label><label>发票方向 / tpNF<select value={form.tp_nf} onChange={e => setForm({ ...form, tp_nf: e.target.value })}><option value="1">1 · Saída 出库</option><option value="0">0 · Entrada 入库</option><option value="0,1">0 和 1</option></select></label><label>发票目的 / finNFe<select value={form.fin_nfe} onChange={e => setForm({ ...form, fin_nfe: e.target.value })}><option value="1">1 · Normal 正常</option><option value="4">4 · Devolução 退货</option><option value="2">2 · Complementar 补充</option><option value="3">3 · Ajuste 调整</option><option value="1,4">1 和 4</option></select></label><label>本公司角色<select disabled={form.result_class === 'sale'} value={form.result_class === 'sale' ? 'issuer' : form.role} onChange={e => setForm({ ...form, role: e.target.value })}><option value="issuer">发行方 / Emitente</option><option value="recipient">接收方 / Destinatário</option></select></label><label>票据模型 / mod<select value={form.model} onChange={e => setForm({ ...form, model: e.target.value })}><option value="55">55 · NF-e</option></select></label><label>分类结果<select value={form.result_class} onChange={e => setForm({ ...form, result_class: e.target.value })}>{classes.map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label><label className="check-row"><input type="checkbox" checked={form.active} onChange={e => setForm({ ...form, active: e.target.checked })} />规则启用</label></div><label className="check-row"><input type="checkbox" checked={ack} onChange={e => setAck(e.target.checked)} /> 已由负责会计确认规则依据与适用范围</label>{error && <div className="alert error">{error}</div>}<div className="modal-actions"><button className="button primary" disabled={busy || !ack}>{busy ? '保存中…' : '保存分类规则'}</button></div></form></Modal>}
  </div>;
}

function AuditPanel() {
  const [rows, setRows] = useState<any[]>([]); const [action, setAction] = useState(''); const [selected, setSelected] = useState<any | null>(null); const [error, setError] = useState('');
  const load = () => api<{ items: any[] }>('/api/audit?limit=500').then(result => setRows(result.items)).catch(e => setError(errorMessage(e))); useEffect(() => { load(); }, []);
  const names: Record<string, string> = { login: '登录', import: '导入', classification: '分类复核', batch_classification: '批量分类', company_confirm: '确认公司', export: 'XML 导出', backup: '备份', resolve_conflict: '冲突处理', coverage: '覆盖确认', user_create: '创建用户', user_update: '更新用户', rule_create: '创建规则', rule_update: '更新规则' };
  const visible = rows.filter(row => !action || row.action === action);
  return <div className="panel history-panel"><div className="panel-title"><div><h2>审计记录</h2><p>最近 500 条 · 只读</p></div><button className="button ghost small" onClick={load}>刷新记录</button></div><div className="settings-filter"><label>操作类型<select value={action} onChange={e => setAction(e.target.value)}><option value="">全部操作</option>{Array.from(new Set(rows.map(row => row.action))).map(value => <option key={value} value={value}>{names[value] || value}</option>)}</select></label></div>{error && <div className="alert error">{error}</div>}<div className="table-scroll"><table><thead><tr><th>时间</th><th>操作</th><th>对象</th><th>原因</th><th /></tr></thead><tbody>{visible.map(row => <tr key={row.id}><td>{dateTime(row.created_at)}</td><td>{names[row.action] || row.action}</td><td>{row.entity_type} #{row.entity_id}</td><td className="wrap-cell">{row.after?.reason || row.after?.notes || '—'}</td><td><button className="button link" onClick={() => setSelected(row)}>详情</button></td></tr>)}</tbody></table>{!visible.length && <div className="empty">当前筛选范围暂无审计记录。</div>}</div>{selected && <Modal title={`审计记录 #${selected.id}`} onClose={() => setSelected(null)}><div className="detail-list"><div><span>时间</span><b>{dateTime(selected.created_at)}</b></div><div><span>操作</span><b>{names[selected.action] || selected.action}</b></div><div><span>对象</span><b>{selected.entity_type} #{selected.entity_id}</b></div></div><div className="detail-section"><h3>变更前</h3><pre className="audit-json">{JSON.stringify(selected.before || {}, null, 2)}</pre><h3>变更后</h3><pre className="audit-json">{JSON.stringify(selected.after || {}, null, 2)}</pre></div></Modal>}</div>;
}

function BackupsPanel() {
  const [backups, setBackups] = useState<Backup[]>([]); const [busy, setBusy] = useState(false); const [verifying, setVerifying] = useState<number | null>(null); const [error, setError] = useState(''); const [message, setMessage] = useState(''); const [verification, setVerification] = useState<any | null>(null);
  const load = () => api<Backup[]>('/api/backups').then(setBackups).catch(e => setError(errorMessage(e))); useEffect(() => { load(); }, []);
  async function create() {
    setBusy(true); setError(''); setMessage('');
    try { const result = await api<Backup>('/api/backups', { method: 'POST' }); setMessage(`备份 ${result.snapshot_id} 已创建。请下载到独立备份目标并进行隔离恢复验证。`); load(); } catch (e) { setError(errorMessage(e)); } finally { setBusy(false); }
  }
  async function verify(backup: Backup) {
    setVerifying(backup.id); setError('');
    try { setVerification(await api(`/api/backups/${backup.id}/verify`, { method: 'POST' })); load(); } catch (e) { setError(errorMessage(e)); } finally { setVerifying(null); }
  }
  return <div className="panel history-panel"><div className="panel-title"><div><h2>完整备份与隔离恢复验证</h2><p>数据库、原始 XML、配置清单及哈希一并保存</p></div><div className="toolbar-actions"><button className="button ghost small" onClick={load}>刷新</button><button className="button primary" onClick={create} disabled={busy}>{busy ? '创建完整备份中…' : '创建完整备份'}</button></div></div><div className="coverage-note">本地备份与业务数据同盘，不能替代独立备份。恢复验证在隔离位置检查备份，不覆盖当前业务数据库。</div>{message && <div className="alert success-alert">{message}</div>}{error && <div className="alert error">{error}</div>}<div className="backup-list">{backups.map(backup => <div className="backup-record" key={backup.id}><div><b>{backup.snapshot_id}</b><small>{dateTime(backup.end_at || backup.start_at)}</small></div><span className={`status ${backup.status === 'completed' ? 'success' : 'warning'}`}>{statusLabel[backup.status] || backup.status}</span><div className="row-actions"><a className="button link" href={`/api/backups/${backup.id}/download`}>下载备份 ZIP</a><button className="button ghost small" onClick={() => verify(backup)} disabled={verifying !== null}>{verifying === backup.id ? '隔离验证中…' : '验证恢复完整性'}</button></div></div>)}{!backups.length && <div className="empty">暂无完整备份。建议导入和复核完成后创建备份。</div>}</div>{verification && <Modal title="备份恢复验证结果" onClose={() => setVerification(null)}><div className="coverage-note">以下为服务端返回的验证证据，当前数据库未被覆盖。</div><pre className="audit-json">{JSON.stringify(verification, null, 2)}</pre></Modal>}</div>;
}

function Modal({ title, onClose, children }: { title: string; onClose: () => void; children: any }) { return <div className="modal-backdrop" role="dialog" aria-modal="true"><div className="modal"><div className="modal-head"><h2>{title}</h2><button className="icon-button" onClick={onClose} aria-label="关闭">×</button></div>{children}</div></div>; }

export default App;
