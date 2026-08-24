import { useEffect, useMemo, useState, type FormEvent, type ReactNode } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Activity,
  Boxes,
  ChevronRight,
  CircleAlert,
  CircleCheck,
  Clock3,
  Cpu,
  GitBranch,
  LayoutDashboard,
  ListChecks,
  LoaderCircle,
  Power,
  Plus,
  RefreshCw,
  Server,
  ShieldCheck,
  Workflow,
  X,
} from 'lucide-react'
import { api } from './api'
import { DelegationsPage } from './DelegationsPage'
import type {
  Decision,
  Delegation,
  Instance,
  Job,
  JobSubmission,
  ManagedService,
  ModelCatalog,
  Template,
} from './types'

type Page = 'jobs' | 'delegations' | 'capabilities' | 'services' | 'templates' | 'decisions'

const statusLabels: Record<string, string> = {
  queued: '排队中',
  waiting_decision: '等待决策',
  running: '执行中',
  succeeded: '已完成',
  failed: '失败',
  cancelled: '已取消',
  reconciliation_required: '需人工对账',
}

function App() {
  const [page, setPage] = useState<Page>('jobs')
  const [composerOpen, setComposerOpen] = useState(false)
  const [selectedJob, setSelectedJob] = useState<Job | null>(null)
  const [selectedDelegationId, setSelectedDelegationId] = useState<string | null>(null)
  const queryClient = useQueryClient()
  const jobsQuery = useQuery({ queryKey: ['jobs'], queryFn: api.jobs, refetchInterval: 2500 })
  const capabilitiesQuery = useQuery({
    queryKey: ['capabilities'],
    queryFn: api.capabilities,
    enabled: page === 'capabilities',
  })
  const templatesQuery = useQuery({
    queryKey: ['templates'],
    queryFn: api.templates,
    enabled: page === 'templates',
  })
  const instancesQuery = useQuery({
    queryKey: ['instances'],
    queryFn: api.instances,
    enabled: page === 'templates',
    refetchInterval: page === 'templates' ? 2500 : false,
  })
  const decisionsQuery = useQuery({
    queryKey: ['decisions'],
    queryFn: api.decisions,
    enabled: page === 'decisions',
    refetchInterval: page === 'decisions' ? 2500 : false,
  })
  const servicesQuery = useQuery({
    queryKey: ['services'],
    queryFn: api.services,
    enabled: page === 'services',
    refetchInterval: page === 'services' ? 2000 : false,
  })
  const delegationsQuery = useQuery({
    queryKey: ['delegations'],
    queryFn: api.delegations,
    enabled: page === 'delegations',
    refetchInterval: page === 'delegations' ? 10_000 : false,
  })
  const modelsQuery = useQuery({
    queryKey: ['models'],
    queryFn: api.models,
    enabled: composerOpen,
    staleTime: 60_000,
  })
  const submitMutation = useMutation({
    mutationFn: api.submit,
    onSuccess: (job) => {
      void queryClient.invalidateQueries({ queryKey: ['jobs'] })
      setComposerOpen(false)
      setSelectedJob(job)
    },
  })
  const cancelMutation = useMutation({
    mutationFn: api.cancel,
    onSuccess: (job) => {
      void queryClient.invalidateQueries({ queryKey: ['jobs'] })
      setSelectedJob(job)
    },
  })
  const decisionMutation = useMutation({
    mutationFn: ({ proposalId, revision, decision }: { proposalId: string; revision: number; decision: 'approved' | 'rejected' }) => api.decide(proposalId, revision, decision),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['decisions'] })
      void queryClient.invalidateQueries({ queryKey: ['instances'] })
    },
  })
  const serviceMutation = useMutation({
    mutationFn: ({ serviceId, action }: { serviceId: string; action: 'start' | 'stop' }) =>
      action === 'start' ? api.startService(serviceId) : api.stopService(serviceId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['services'] })
    },
  })
  const jobs = jobsQuery.data ?? []
  const runningCount = jobs.filter((job) => job.status === 'running').length
  const terminalCount = jobs.filter((job) => ['succeeded', 'failed', 'cancelled'].includes(job.status)).length
  const delegations = delegationsQuery.data ?? []
  const selectedDelegation = selectedDelegationId ? delegations.find((delegation) => delegation.delegation_id === selectedDelegationId) ?? null : null
  const updateDelegationSnapshot = (snapshot: Delegation) => {
    queryClient.setQueryData<Delegation[]>(['delegations'], (current) =>
      current?.map((delegation) =>
        delegation.delegation_id === snapshot.delegation_id ? snapshot : delegation,
      ) ?? current,
    )
  }

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark"><Activity size={18} /></div>
          <div><strong>Misaka</strong><span>Multi-Agent V3</span></div>
        </div>
        <div className="workspace-switcher"><span className="status-dot" /> Local workspace <ChevronRight size={14} /></div>
        <nav className="nav-list">
          <button className={page === 'jobs' ? 'nav-item active' : 'nav-item'} onClick={() => setPage('jobs')}><LayoutDashboard size={17} />执行中心</button>
          <button className={page === 'delegations' ? 'nav-item active' : 'nav-item'} onClick={() => setPage('delegations')}><GitBranch size={17} />委派状态</button>
          <button className={page === 'capabilities' ? 'nav-item active' : 'nav-item'} onClick={() => setPage('capabilities')}><Boxes size={17} />能力目录</button>
          <button className={page === 'services' ? 'nav-item active' : 'nav-item'} onClick={() => setPage('services')}><Server size={17} />服务管理</button>
          <button className={page === 'templates' ? 'nav-item active' : 'nav-item'} onClick={() => setPage('templates')}><Workflow size={17} />模板与实例</button>
          <button className={page === 'decisions' ? 'nav-item active' : 'nav-item'} onClick={() => setPage('decisions')}><ShieldCheck size={17} />决策中心</button>
        </nav>
        <div className="sidebar-footer"><span className="status-dot" />Control Plane online</div>
      </aside>

      <main className="main-content">
        <header className="topbar">
          <div><span className="eyebrow">LOCAL CONTROL PLANE</span><h1>{page === 'jobs' ? '执行中心' : page === 'delegations' ? '委派状态' : page === 'capabilities' ? '能力目录' : page === 'services' ? '服务管理' : page === 'templates' ? '模板与实例' : '决策中心'}</h1></div>
          {page === 'jobs' && <button className="primary-button" onClick={() => setComposerOpen(true)}><Plus size={17} />新建任务</button>}
        </header>

        {page === 'jobs' ? (
          <>
            <section className="metric-grid">
              <Metric icon={<ListChecks size={18} />} label="全部任务" value={jobs.length} />
              <Metric icon={<LoaderCircle size={18} />} label="正在执行" value={runningCount} tone="blue" />
              <Metric icon={<CircleCheck size={18} />} label="已结束" value={terminalCount} tone="green" />
            </section>
            <section className="panel jobs-panel">
              <div className="panel-header"><div><h2>任务实例</h2><p>每个任务由显式模型、推理等级和能力操作组成。</p></div><button className="icon-button" onClick={() => void queryClient.invalidateQueries({ queryKey: ['jobs'] })} title="刷新"><RefreshCw size={16} /></button></div>
              {jobsQuery.isLoading ? <EmptyState icon={<LoaderCircle className="spin" />} title="正在加载任务" /> : jobs.length === 0 ? <EmptyState icon={<Clock3 />} title="还没有任务" description="创建一个任务开始执行。" /> : <div className="job-table"><div className="table-head"><span>任务</span><span>能力 / 操作</span><span>模型</span><span>推理等级</span><span /></div>{jobs.map((job) => <JobRow key={job.job_id} job={job} onClick={() => setSelectedJob(job)} />)}</div>}
            </section>
          </>
        ) : page === 'delegations' ? (
          <DelegationsPage delegations={delegations} selectedDelegation={selectedDelegation} loading={delegationsQuery.isLoading} error={delegationsQuery.error?.message} onRefresh={() => void queryClient.invalidateQueries({ queryKey: ['delegations'] })} onSelect={setSelectedDelegationId} onSnapshot={updateDelegationSnapshot} />
        ) : page === 'capabilities' ? (
          <section className="panel capability-panel"><div className="panel-header"><div><h2>已注册能力</h2><p>由当前 Control Plane 进程中的 InvocationRuntime 提供。</p></div></div>{capabilitiesQuery.isLoading ? <EmptyState icon={<LoaderCircle className="spin" />} title="正在加载能力" /> : (capabilitiesQuery.data ?? []).map((capability) => <div className="capability-card" key={capability.capability_id}><div className="capability-icon"><Cpu size={19} /></div><div><h3>{capability.capability_id}</h3><p>版本 {capability.version} · 操作 {capability.operations.join(', ')}</p><div className="tag-row">{capability.features.map((feature) => <span className="tag" key={feature}>{feature}</span>)}</div></div></div>)}</section>
        ) : page === 'services' ? (
          <ServicesPage services={servicesQuery.data ?? []} loading={servicesQuery.isLoading} error={servicesQuery.error?.message} pending={serviceMutation.isPending} actionError={serviceMutation.error?.message} onAction={(serviceId, action) => serviceMutation.mutate({ serviceId, action })} onRefresh={() => void queryClient.invalidateQueries({ queryKey: ['services'] })} />
        ) : page === 'templates' ? (
          <TemplatePage templates={templatesQuery.data ?? []} instances={instancesQuery.data ?? []} loading={templatesQuery.isLoading || instancesQuery.isLoading} />
        ) : (
          <DecisionPage decisions={decisionsQuery.data ?? []} loading={decisionsQuery.isLoading} onDecision={(proposalId, revision, decision) => decisionMutation.mutate({ proposalId, revision, decision })} pending={decisionMutation.isPending} />
        )}
      </main>

      {composerOpen && <JobComposer catalogs={modelsQuery.data ?? []} modelsLoading={modelsQuery.isLoading} modelsError={modelsQuery.error?.message} submitting={submitMutation.isPending} onClose={() => setComposerOpen(false)} onSubmit={(payload) => submitMutation.mutate(payload)} error={submitMutation.error?.message} />}
      {selectedJob && <JobDrawer job={selectedJob} cancelling={cancelMutation.isPending} onClose={() => setSelectedJob(null)} onCancel={() => cancelMutation.mutate(selectedJob.job_id)} />}
    </div>
  )
}

function Metric({ icon, label, value, tone = '' }: { icon: ReactNode; label: string; value: number; tone?: string }) {
  return <div className={'metric-card ' + tone}><div className="metric-icon">{icon}</div><div><span>{label}</span><strong>{value}</strong></div></div>
}

function JobRow({ job, onClick }: { job: Job; onClick: () => void }) {
  return <button className="job-row" onClick={onClick}><div className="job-name"><span className={'status-badge ' + job.status}><StatusIcon status={job.status} />{statusLabels[job.status] ?? job.status}</span><strong>{job.job_id}</strong><small>v{job.version}</small></div><span className="muted">{String(job.request.capability_id)} / {String(job.request.operation)}</span><span className="muted">{String(job.request.model ?? '—')}</span><span className="muted">{String(job.request.effort ?? '—')}</span><ChevronRight size={16} className="row-arrow" /></button>
}

function StatusIcon({ status }: { status: string }) {
  if (status === 'succeeded') return <CircleCheck size={14} />
  if (status === 'failed' || status === 'reconciliation_required') return <CircleAlert size={14} />
  if (status === 'running') return <LoaderCircle size={14} className="spin" />
  return <Clock3 size={14} />
}

function EmptyState({ icon, title, description }: { icon: ReactNode; title: string; description?: string }) {
  return <div className="empty-state"><div>{icon}</div><strong>{title}</strong>{description && <p>{description}</p>}</div>
}

const serviceStatusLabels: Record<ManagedService['status'], string> = {
  stopped: '已停止',
  starting: '启动中',
  running: '运行中',
  stopping: '停止中',
  failed: '失败',
}

function ServicesPage({ services, loading, error, pending, actionError, onAction, onRefresh }: {
  services: ManagedService[]
  loading: boolean
  error?: string
  pending: boolean
  actionError?: string
  onAction: (serviceId: string, action: 'start' | 'stop') => void
  onRefresh: () => void
}) {
  return <section className="panel capability-panel"><div className="panel-header"><div><h2>支持的服务</h2><p>服务由当前 Profile 静态注册，启动和停止不会动态加载新的模块。</p></div><button className="icon-button" onClick={onRefresh} title="刷新"><RefreshCw size={16} /></button></div>{error && <div className="error-banner">服务目录读取失败：{error}</div>}{actionError && <div className="error-banner">服务操作失败：{actionError}</div>}{loading ? <EmptyState icon={<LoaderCircle className="spin" />} title="正在加载服务" /> : services.length === 0 ? <EmptyState icon={<Server />} title="当前没有可管理服务" description="请使用带服务目录的 Control Plane Profile。" /> : services.map((service) => <ServiceCard key={service.service_id} service={service} pending={pending} onAction={onAction} />)}</section>
}

function ServiceCard({ service, pending, onAction }: { service: ManagedService; pending: boolean; onAction: (serviceId: string, action: 'start' | 'stop') => void }) {
  const busy = pending || service.status === 'starting' || service.status === 'stopping'
  const running = service.status === 'running'
  return <div className="service-card"><div className="capability-icon"><Server size={19} /></div><div className="service-card-main"><div className="service-card-title"><div><h3>{service.display_name}</h3><span className="service-id">{service.service_id}</span></div><span className={'status-badge ' + service.status}><StatusIcon status={service.status} />{serviceStatusLabels[service.status]}</span></div><p>{service.description}</p><div className="service-meta"><span>{service.category}</span>{service.endpoint && <a href={service.endpoint} target="_blank" rel="noreferrer">{service.endpoint}</a>}{service.pid && <span>PID {service.pid}</span>}</div>{service.last_error && <div className="error-banner"><strong>最近错误</strong><span>{service.last_error}</span></div>}{service.recent_output.length > 0 && <details className="service-output"><summary>查看最近日志</summary><pre>{service.recent_output.join('\n')}</pre></details>}<div className="service-actions">{service.controllable && (running ? <button className="danger-button" disabled={busy} onClick={() => onAction(service.service_id, 'stop')}><Power size={15} />停止服务</button> : <button className="primary-button" disabled={busy} onClick={() => onAction(service.service_id, 'start')}><Power size={15} />启动服务</button>)}</div></div></div>
}

function TemplatePage({ templates, instances, loading }: { templates: Template[]; instances: Instance[]; loading: boolean }) {
  return <section className="panel capability-panel"><div className="panel-header"><div><h2>模板版本</h2><p>模板是不可变定义，实例记录引用固定的模板版本。</p></div></div>{loading ? <EmptyState icon={<LoaderCircle className="spin" />} title="正在加载模板" /> : templates.length === 0 ? <EmptyState icon={<Workflow />} title="还没有模板" description="通过 Control Plane API 导入或创建模板。" /> : templates.map((template) => <div className="capability-card" key={template.template_id + ':' + template.version}><div className="capability-icon"><Workflow size={19} /></div><div><h3>{template.name}</h3><p>{template.template_id} · v{template.version} · {template.coordinator.toUpperCase()} · {template.nodes.length} 个节点</p><div className="tag-row"><span className="tag">{template.decision_required ? '需要决策' : '直接执行'}</span><span className="tag">实例 {instances.filter((instance) => instance.template_id === template.template_id && instance.template_version === template.version).length}</span></div></div></div>)}<div className="panel-header"><div><h2>执行实例</h2><p>实例状态可以在服务重启后从 Durable Log 恢复。</p></div></div>{instances.length === 0 ? <EmptyState icon={<Clock3 />} title="还没有实例" /> : instances.map((instance) => <div className="capability-card" key={instance.instance_id}><div className="capability-icon"><StatusIcon status={instance.status} /></div><div><h3>{instance.instance_id}</h3><p>{instance.template_id} · v{instance.template_version}</p><span className={'status-badge ' + instance.status}>{statusLabels[instance.status] ?? instance.status}</span></div></div>)}</section>
}

function DecisionPage({ decisions, loading, onDecision, pending }: { decisions: Decision[]; loading: boolean; onDecision: (proposalId: string, revision: number, decision: 'approved' | 'rejected') => void; pending: boolean }) {
  return <section className="panel capability-panel"><div className="panel-header"><div><h2>人工决策</h2><p>每个决定严格绑定计划版本、效果范围和决策主体。</p></div></div>{loading ? <EmptyState icon={<LoaderCircle className="spin" />} title="正在加载决策" /> : decisions.length === 0 ? <EmptyState icon={<ShieldCheck />} title="没有决策记录" /> : decisions.map((decision) => <div className="capability-card" key={decision.proposal_id + ':' + decision.revision}><div className="capability-icon"><ShieldCheck size={19} /></div><div style={{ flex: 1 }}><h3>{decision.instance_id || decision.proposal_id}</h3><p>{decision.proposal_id} · revision {decision.revision} · {decision.status === 'pending' ? '等待决定' : decision.status}</p><div className="tag-row"><span className="tag">scope {decision.scope_id}</span>{decision.requested_effects.map((effect) => <span className="tag" key={effect}>{effect}</span>)}</div><p className="muted">plan {decision.plan_hash.slice(0, 12)}{decision.decided_by ? ' · by ' + decision.decided_by : ''}</p>{decision.status === 'pending' ? <div className="modal-actions"><button className="secondary-button" onClick={() => onDecision(decision.proposal_id, decision.revision, 'rejected')} disabled={pending}>拒绝</button><button className="primary-button" onClick={() => onDecision(decision.proposal_id, decision.revision, 'approved')} disabled={pending}>批准</button></div> : decision.reason && <span className="muted">{decision.reason}</span>}</div></div>)}</section>
}

function JobComposer({ catalogs, modelsLoading, modelsError, submitting, error, onClose, onSubmit }: { catalogs: ModelCatalog[]; modelsLoading: boolean; modelsError?: string; submitting: boolean; error?: string; onClose: () => void; onSubmit: (payload: JobSubmission) => void }) {
  const [jobId, setJobId] = useState('job-' + Date.now().toString(36))
  const [providerId, setProviderId] = useState('')
  const [model, setModel] = useState('')
  const [effort, setEffort] = useState('')
  const [prompt, setPrompt] = useState('')
  const options = useMemo(() => catalogs.flatMap((catalog) => catalog.models.map((item) => ({ provider_id: catalog.provider_id, model: item }))), [catalogs])
  const selected = options.find((option) => option.provider_id === providerId && option.model.model_id === model) ?? options[0]
  const efforts = selected?.model.supported_efforts ?? []
  useEffect(() => {
    if (!selected) {
      setProviderId('')
      setModel('')
      setEffort('')
      return
    }
    if (selected.provider_id !== providerId || selected.model.model_id !== model) {
      setProviderId(selected.provider_id)
      setModel(selected.model.model_id)
    }
    if (!efforts.includes(effort)) setEffort(efforts[0] ?? '')
  }, [effort, efforts, model, providerId, selected])
  const submit = (event: FormEvent) => {
    event.preventDefault()
    if (!selected || !effort) return
    onSubmit({ job_id: jobId, idempotency_key: jobId, capability_id: 'agent.invocation', operation: 'invoke', input: { prompt }, model: selected.model.model_id, effort, network_policy: 'deny', provider_id: selected.provider_id, output_schema: { type: 'object', properties: { answer: { type: 'string' } }, required: ['answer'], additionalProperties: false } })
  }
  return <div className="modal-backdrop" onMouseDown={(event) => event.target === event.currentTarget && onClose()}><form className="modal" onSubmit={submit}><div className="modal-header"><div><span className="eyebrow">NEW INVOCATION</span><h2>创建任务</h2></div><button type="button" className="icon-button" onClick={onClose}><X size={18} /></button></div><label>任务 ID<input value={jobId} onChange={(event) => setJobId(event.target.value)} required /></label><div className="form-grid"><label>模型<select value={selected ? selected.provider_id + ':' + selected.model.model_id : ''} onChange={(event) => { const option = options.find((item) => item.provider_id + ':' + item.model.model_id === event.target.value); setProviderId(option?.provider_id ?? ''); setModel(option?.model.model_id ?? ''); setEffort(option?.model.supported_efforts[0] ?? '') }} disabled={modelsLoading || options.length === 0} required><option value="">{modelsLoading ? '正在读取模型目录…' : options.length === 0 ? '没有可用模型' : '选择模型'}</option>{options.map((option) => <option key={option.provider_id + ':' + option.model.model_id} value={option.provider_id + ':' + option.model.model_id}>{option.model.display_name} · {option.model.model_id} · {option.provider_id}</option>)}</select></label><label>推理等级<select value={effort} onChange={(event) => setEffort(event.target.value)} disabled={!selected || efforts.length === 0} required>{efforts.map((value) => <option key={value} value={value}>{value}</option>)}</select></label></div>{selected?.model.description && <p className="form-hint">{selected.model.description}</p>}{modelsError && <div className="error-banner">模型目录读取失败：{modelsError}</div>}<label>任务内容<textarea value={prompt} onChange={(event) => setPrompt(event.target.value)} placeholder="描述希望 Agent 执行的内容…" rows={6} required /></label>{error && <div className="error-banner">{error}</div>}<div className="modal-actions"><button type="button" className="secondary-button" onClick={onClose}>取消</button><button className="primary-button" disabled={submitting || !selected || !effort || modelsLoading}>{submitting ? <LoaderCircle className="spin" size={16} /> : <Activity size={16} />}运行任务</button></div></form></div>
}

function JobDrawer({ job, cancelling, onClose, onCancel }: { job: Job; cancelling: boolean; onClose: () => void; onCancel: () => void }) {
  const terminal = ['succeeded', 'failed', 'cancelled', 'reconciliation_required'].includes(job.status)
  const resultText = useMemo(() => job.result ? JSON.stringify(job.result, null, 2) : '', [job.result])
  return <div className="drawer-backdrop" onMouseDown={(event) => event.target === event.currentTarget && onClose()}><aside className="drawer"><div className="drawer-header"><div><span className="eyebrow">JOB INSTANCE</span><h2>{job.job_id}</h2></div><button className="icon-button" onClick={onClose}><X size={18} /></button></div><div className="drawer-status"><span className={'status-badge ' + job.status}><StatusIcon status={job.status} />{statusLabels[job.status] ?? job.status}</span><span className="muted">版本 {job.version}</span></div><dl className="detail-list"><div><dt>能力</dt><dd>{String(job.request.capability_id)} / {String(job.request.operation)}</dd></div><div><dt>模型</dt><dd>{String(job.request.model ?? '—')} · {String(job.request.effort ?? '—')}</dd></div><div><dt>幂等键</dt><dd>{job.idempotency_key}</dd></div></dl>{job.error_message && <div className="error-banner"><strong>{job.error_code}</strong><span>{job.error_message}</span></div>}{resultText && <div className="result-block"><div className="result-title">输出</div><pre>{resultText}</pre></div>}<div className="drawer-actions">{!terminal && <button className="danger-button" onClick={onCancel} disabled={cancelling}>{cancelling ? <LoaderCircle className="spin" size={16} /> : <CircleAlert size={16} />}取消任务</button>}<button className="secondary-button" onClick={onClose}>关闭</button></div></aside></div>
}

export default App
