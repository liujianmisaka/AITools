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
  LayoutDashboard,
  ListChecks,
  LoaderCircle,
  Plus,
  RefreshCw,
  X,
} from 'lucide-react'
import { api } from './api'
import type { Job, JobSubmission, ModelCatalog } from './types'

type Page = 'jobs' | 'capabilities'

const statusLabels: Record<string, string> = {
  queued: '排队中',
  waiting_approval: '等待审批',
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
  const queryClient = useQueryClient()
  const jobsQuery = useQuery({ queryKey: ['jobs'], queryFn: api.jobs, refetchInterval: 2500 })
  const capabilitiesQuery = useQuery({
    queryKey: ['capabilities'],
    queryFn: api.capabilities,
    enabled: page === 'capabilities',
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
  const jobs = jobsQuery.data ?? []
  const runningCount = jobs.filter((job) => job.status === 'running').length
  const terminalCount = jobs.filter((job) => ['succeeded', 'failed', 'cancelled'].includes(job.status)).length

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
          <button className={page === 'capabilities' ? 'nav-item active' : 'nav-item'} onClick={() => setPage('capabilities')}><Boxes size={17} />能力目录</button>
        </nav>
        <div className="sidebar-footer"><span className="status-dot" />Control Plane online</div>
      </aside>

      <main className="main-content">
        <header className="topbar">
          <div><span className="eyebrow">LOCAL CONTROL PLANE</span><h1>{page === 'jobs' ? '执行中心' : '能力目录'}</h1></div>
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
        ) : (
          <section className="panel capability-panel"><div className="panel-header"><div><h2>已注册能力</h2><p>由当前 Control Plane 进程中的 InvocationRuntime 提供。</p></div></div>{capabilitiesQuery.isLoading ? <EmptyState icon={<LoaderCircle className="spin" />} title="正在加载能力" /> : (capabilitiesQuery.data ?? []).map((capability) => <div className="capability-card" key={capability.capability_id}><div className="capability-icon"><Cpu size={19} /></div><div><h3>{capability.capability_id}</h3><p>版本 {capability.version} · 操作 {capability.operations.join(', ')}</p><div className="tag-row">{capability.features.map((feature) => <span className="tag" key={feature}>{feature}</span>)}</div></div></div>)}</section>
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
    onSubmit({ job_id: jobId, idempotency_key: jobId, capability_id: 'agent.invocation', operation: 'invoke', input: { prompt }, model: selected.model.model_id, effort, provider_id: selected.provider_id, output_schema: { type: 'object', properties: { answer: { type: 'string' } }, required: ['answer'], additionalProperties: false } })
  }
  return <div className="modal-backdrop" onMouseDown={(event) => event.target === event.currentTarget && onClose()}><form className="modal" onSubmit={submit}><div className="modal-header"><div><span className="eyebrow">NEW INVOCATION</span><h2>创建任务</h2></div><button type="button" className="icon-button" onClick={onClose}><X size={18} /></button></div><label>任务 ID<input value={jobId} onChange={(event) => setJobId(event.target.value)} required /></label><div className="form-grid"><label>模型<select value={selected ? selected.provider_id + ':' + selected.model.model_id : ''} onChange={(event) => { const option = options.find((item) => item.provider_id + ':' + item.model.model_id === event.target.value); setProviderId(option?.provider_id ?? ''); setModel(option?.model.model_id ?? ''); setEffort(option?.model.supported_efforts[0] ?? '') }} disabled={modelsLoading || options.length === 0} required><option value="">{modelsLoading ? '正在读取模型目录…' : options.length === 0 ? '没有可用模型' : '选择模型'}</option>{options.map((option) => <option key={option.provider_id + ':' + option.model.model_id} value={option.provider_id + ':' + option.model.model_id}>{option.model.display_name} · {option.model.model_id} · {option.provider_id}</option>)}</select></label><label>推理等级<select value={effort} onChange={(event) => setEffort(event.target.value)} disabled={!selected || efforts.length === 0} required>{efforts.map((value) => <option key={value} value={value}>{value}</option>)}</select></label></div>{selected?.model.description && <p className="form-hint">{selected.model.description}</p>}{modelsError && <div className="error-banner">模型目录读取失败：{modelsError}</div>}<label>任务内容<textarea value={prompt} onChange={(event) => setPrompt(event.target.value)} placeholder="描述希望 Agent 执行的内容…" rows={6} required /></label>{error && <div className="error-banner">{error}</div>}<div className="modal-actions"><button type="button" className="secondary-button" onClick={onClose}>取消</button><button className="primary-button" disabled={submitting || !selected || !effort || modelsLoading}>{submitting ? <LoaderCircle className="spin" size={16} /> : <Activity size={16} />}运行任务</button></div></form></div>
}

function JobDrawer({ job, cancelling, onClose, onCancel }: { job: Job; cancelling: boolean; onClose: () => void; onCancel: () => void }) {
  const terminal = ['succeeded', 'failed', 'cancelled', 'reconciliation_required'].includes(job.status)
  const resultText = useMemo(() => job.result ? JSON.stringify(job.result, null, 2) : '', [job.result])
  return <div className="drawer-backdrop" onMouseDown={(event) => event.target === event.currentTarget && onClose()}><aside className="drawer"><div className="drawer-header"><div><span className="eyebrow">JOB INSTANCE</span><h2>{job.job_id}</h2></div><button className="icon-button" onClick={onClose}><X size={18} /></button></div><div className="drawer-status"><span className={'status-badge ' + job.status}><StatusIcon status={job.status} />{statusLabels[job.status] ?? job.status}</span><span className="muted">版本 {job.version}</span></div><dl className="detail-list"><div><dt>能力</dt><dd>{String(job.request.capability_id)} / {String(job.request.operation)}</dd></div><div><dt>模型</dt><dd>{String(job.request.model ?? '—')} · {String(job.request.effort ?? '—')}</dd></div><div><dt>幂等键</dt><dd>{job.idempotency_key}</dd></div></dl>{job.error_message && <div className="error-banner"><strong>{job.error_code}</strong><span>{job.error_message}</span></div>}{resultText && <div className="result-block"><div className="result-title">输出</div><pre>{resultText}</pre></div>}<div className="drawer-actions">{!terminal && <button className="danger-button" onClick={onCancel} disabled={cancelling}>{cancelling ? <LoaderCircle className="spin" size={16} /> : <CircleAlert size={16} />}取消任务</button>}<button className="secondary-button" onClick={onClose}>关闭</button></div></aside></div>
}

export default App
