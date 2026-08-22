import type { ReactNode } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Activity,
  AlertTriangle,
  Boxes,
  CircleStop,
  Clock3,
  ExternalLink,
  Gauge,
  Layers3,
  Link2Off,
  LoaderCircle,
  Play,
  RefreshCw,
  Server,
  ShieldCheck,
  Square,
  SquareTerminal,
  Unplug,
  Wifi,
  WifiOff,
  Zap,
} from 'lucide-react'
import { api } from './api'
import type {
  LaunchMode,
  ManagedService,
  ServiceAction,
  ServiceActionRequest,
  ServiceGroup,
  ServiceScope,
  ServiceStatus,
} from './types'

const serviceStatusLabels: Record<ServiceStatus, string> = {
  stopped: '已停止',
  starting: '启动中',
  running: '运行中',
  stopping: '停止中',
  failed: '失败',
  unavailable: '等待依赖',
  on_demand: '按需启动',
}

const scopeLabels: Record<ServiceScope, string> = {
  aitools: 'AITools 托管',
  control_plane: 'Control Plane 委派',
  client: '客户端生命周期',
}

const launchModeLabels: Record<LaunchMode, string> = {
  managed: '进程托管',
  delegated: '下游委派',
  on_demand: '按需进程',
}

const dateTimeFormatter = new Intl.DateTimeFormat('zh-CN', {
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  hour12: false,
})

function App() {
  const queryClient = useQueryClient()
  const configurationQuery = useQuery({
    queryKey: ['configuration'],
    queryFn: api.configuration,
    staleTime: Number.POSITIVE_INFINITY,
  })
  const servicesQuery = useQuery({
    queryKey: ['services'],
    queryFn: api.services,
    refetchInterval: 2000,
  })
  const serviceMutation = useMutation({
    mutationFn: ({ service, action }: ServiceActionRequest) =>
      api.changeServiceState(service.service_id, action, service.epoch),
    onSuccess: (updated) => {
      queryClient.setQueryData<ManagedService[]>(['services'], (current) =>
        current?.map((service) =>
          service.service_id === updated.service_id ? updated : service,
        ),
      )
    },
    onSettled: () => queryClient.invalidateQueries({ queryKey: ['services'] }),
  })
  const groupMutation = useMutation({
    mutationFn: ({ groupId, action }: { groupId: ServiceGroup; action: ServiceAction }) =>
      api.changeGroup(groupId, action),
    onSuccess: (result) => queryClient.setQueryData(['services'], result.services),
    onSettled: () => queryClient.invalidateQueries({ queryKey: ['services'] }),
  })
  const services = servicesQuery.data ?? []
  const runningCount = services.filter((service) => service.status === 'running').length
  const transitioningCount = services.filter((service) =>
    ['starting', 'stopping'].includes(service.status),
  ).length
  const unavailableCount = services.filter(
    (service) => service.status === 'unavailable',
  ).length
  const failedCount = services.filter((service) => service.status === 'failed').length
  const pendingRequest = serviceMutation.isPending ? serviceMutation.variables : null
  const busy = serviceMutation.isPending || groupMutation.isPending
  const configuration = configurationQuery.data

  function runAction(service: ManagedService, action: ServiceAction) {
    serviceMutation.reset()
    groupMutation.reset()
    serviceMutation.mutate({ service, action })
  }

  function runGroup(groupId: ServiceGroup, action: ServiceAction) {
    serviceMutation.reset()
    groupMutation.reset()
    groupMutation.mutate({ groupId, action })
  }

  function refresh() {
    serviceMutation.reset()
    groupMutation.reset()
    void Promise.all([configurationQuery.refetch(), servicesQuery.refetch()])
  }

  return (
    <main className="shell">
      <header className="hero">
        <div>
          <div className="brand-line">
            <span className="brand-mark">
              <ShieldCheck size={18} />
            </span>
            <span>AITools / Bootstrap Management</span>
          </div>
          <h1>AITools 服务控制台</h1>
          <p>
            这是独立于 Control Plane 的引导管理面。它负责启动核心服务、主控制台和下游
            A2A 服务，同时保留 epoch fencing，且不允许页面提交任意命令或进程参数。
          </p>
        </div>
        <div className="hero-actions">
          <ConnectionState
            loading={servicesQuery.isLoading}
            error={servicesQuery.error?.message}
          />
          {configuration && (
            <span className="profile-pill">{configuration.profile.toUpperCase()} PROFILE</span>
          )}
          <button
            className="refresh-button"
            type="button"
            onClick={refresh}
            disabled={servicesQuery.isFetching}
          >
            <RefreshCw className={servicesQuery.isFetching ? 'spin' : ''} size={16} />
            刷新状态
          </button>
        </div>
      </header>

      <section className="group-toolbar" aria-label="服务组操作">
        <div>
          <span className="section-kicker">BOOTSTRAP ACTIONS</span>
          <strong>统一启动与收口</strong>
          <p>核心组包含 Control Plane 和主 Web；全部服务还包含下游 A2A 服务。</p>
        </div>
        <div className="group-actions">
          <button
            className="group-button secondary"
            type="button"
            disabled={busy}
            onClick={() => runGroup('core', 'start')}
          >
            <Layers3 size={16} /> 启动核心
          </button>
          <button
            className="group-button primary"
            type="button"
            disabled={busy}
            onClick={() => runGroup('all', 'start')}
          >
            {groupMutation.isPending && groupMutation.variables?.action === 'start' ? (
              <LoaderCircle className="spin" size={16} />
            ) : (
              <Play size={16} />
            )}
            启动全部
          </button>
          <button
            className="group-button danger"
            type="button"
            disabled={busy}
            onClick={() => runGroup('all', 'stop')}
          >
            {groupMutation.isPending && groupMutation.variables?.action === 'stop' ? (
              <LoaderCircle className="spin" size={16} />
            ) : (
              <Square size={15} />
            )}
            停止全部
          </button>
        </div>
      </section>

      <section className="metric-grid" aria-label="服务状态概览">
        <Metric icon={<Boxes size={19} />} label="统一目录" value={services.length} />
        <Metric
          icon={<Activity size={19} />}
          label="正在运行"
          value={runningCount}
          tone="green"
        />
        <Metric
          icon={<Clock3 size={19} />}
          label="状态切换"
          value={transitioningCount}
          tone="blue"
        />
        <Metric
          icon={failedCount > 0 ? <AlertTriangle size={19} /> : <Unplug size={19} />}
          label={failedCount > 0 ? '需要关注' : '等待依赖'}
          value={failedCount > 0 ? failedCount : unavailableCount}
          tone={failedCount > 0 ? 'red' : ''}
        />
      </section>

      <section className="service-surface">
        <div className="surface-header">
          <div>
            <span className="section-kicker">AITools SERVICE CATALOG</span>
            <h2>全部服务</h2>
          </div>
          <div className="polling-note">
            <Gauge size={15} /> 每 2 秒同步一次
            {servicesQuery.dataUpdatedAt > 0 && (
              <span>· 最近更新 {formatDateTime(servicesQuery.dataUpdatedAt)}</span>
            )}
          </div>
        </div>

        {servicesQuery.error && (
          <Notice title="无法读取 AITools 服务目录">{servicesQuery.error.message}</Notice>
        )}
        {serviceMutation.error && (
          <Notice title="服务操作未完成">
            {serviceMutation.error.message}。状态已重新同步，请按最新 epoch 重试。
          </Notice>
        )}
        {groupMutation.error && (
          <Notice title="服务组操作未完成">
            {groupMutation.error.message}。已完成的服务不会被隐式回滚，请以当前目录状态为准。
          </Notice>
        )}

        {servicesQuery.isLoading ? (
          <EmptyState
            icon={<LoaderCircle className="spin" size={26} />}
            title="正在连接 AITools Management API"
            description="管理面就绪后会显示完整服务目录。"
          />
        ) : services.length === 0 ? (
          <EmptyState
            icon={<Server size={26} />}
            title="当前没有登记服务"
            description="请检查 Management API 的静态服务配置。"
          />
        ) : (
          <div className="service-grid">
            {services.map((service) => (
              <ServiceCard
                key={service.service_id}
                service={service}
                pendingAction={
                  pendingRequest?.service.service_id === service.service_id
                    ? pendingRequest.action
                    : null
                }
                globallyBusy={groupMutation.isPending}
                onAction={runAction}
              />
            ))}
          </div>
        )}
      </section>
    </main>
  )
}

function ConnectionState({ loading, error }: { loading: boolean; error?: string }) {
  if (loading) {
    return (
      <div className="connection-pill pending">
        <LoaderCircle className="spin" size={15} /> 正在连接
      </div>
    )
  }
  if (error) {
    return (
      <div className="connection-pill offline" title={error}>
        <WifiOff size={15} /> 管理面异常
      </div>
    )
  }
  return (
    <div className="connection-pill online">
      <Wifi size={15} /> AITools 管理面已连接
    </div>
  )
}

function Metric({
  icon,
  label,
  value,
  tone = '',
}: {
  icon: ReactNode
  label: string
  value: number
  tone?: string
}) {
  return (
    <article className={'metric-card ' + tone}>
      <div className="metric-icon">{icon}</div>
      <div>
        <span>{label}</span>
        <strong>{value}</strong>
      </div>
    </article>
  )
}

function ServiceCard({
  service,
  pendingAction,
  globallyBusy,
  onAction,
}: {
  service: ManagedService
  pendingAction: ServiceAction | null
  globallyBusy: boolean
  onAction: (service: ManagedService, action: ServiceAction) => void
}) {
  const transitioning = service.status === 'starting' || service.status === 'stopping'
  const busy = pendingAction !== null || transitioning || globallyBusy
  const canStart =
    service.controllable && ['stopped', 'failed', 'unavailable'].includes(service.status)
  const canStop = service.controllable && service.status === 'running'
  const lifecycleNote = lifecycleDescription(service)

  return (
    <article className={'service-card status-' + service.status}>
      <div className="card-accent" />
      <div className="service-heading">
        <div className="service-identity">
          <div className="service-icon">
            <ServiceIcon service={service} />
          </div>
          <div>
            <div className="category-row">
              <span className="category-tag">{service.category}</span>
              <span className="scope-tag">{scopeLabels[service.scope]}</span>
              <span className="service-id">{service.service_id}</span>
            </div>
            <h3>{service.display_name}</h3>
          </div>
        </div>
        <StatusBadge status={service.status} />
      </div>

      <p className="service-description">{service.description}</p>

      <dl className="service-details">
        <Detail label="Endpoint">
          {service.endpoint ? (
            <a href={service.endpoint} target="_blank" rel="noreferrer">
              {service.endpoint} <ExternalLink size={12} />
            </a>
          ) : (
            'stdio / client'
          )}
        </Detail>
        <Detail label="Process">
          {service.pid === null ? '未运行' : 'PID ' + service.pid}
        </Detail>
        <Detail label="Epoch">
          <span className="epoch-value">{service.epoch}</span>
        </Detail>
        <Detail label="生命周期">{launchModeLabels[service.launch_mode]}</Detail>
        <Detail label="依赖">
          {service.depends_on.length === 0 ? '无' : service.depends_on.join(', ')}
        </Detail>
        <Detail label="最近停止">{formatOptionalDateTime(service.stopped_at)}</Detail>
      </dl>

      {service.status === 'failed' && service.last_error && (
        <Notice title="最近错误">{service.last_error}</Notice>
      )}
      {service.status === 'unavailable' && (
        <div className="dependency-note">
          <Link2Off size={15} />
          <div>
            <strong>等待上游服务</strong>
            <span>{service.last_error ?? lifecycleNote}</span>
          </div>
        </div>
      )}

      {service.recent_output.length > 0 && (
        <details className="log-block">
          <summary>
            <SquareTerminal size={15} /> 最近日志
            <span>{service.recent_output.length} 行</span>
          </summary>
          <pre>{service.recent_output.join('\n')}</pre>
        </details>
      )}

      <div className="service-footer">
        <span className="fencing-note">
          {service.status === 'on_demand' ? <Zap size={14} /> : <ShieldCheck size={14} />}
          {lifecycleNote}
        </span>
        <div className="service-actions">
          {!service.controllable ? (
            <span className="readonly-label">
              {service.status === 'on_demand' ? '由客户端启动' : '仅观察'}
            </span>
          ) : canStop ? (
            <button
              className="action-button stop"
              type="button"
              disabled={busy}
              onClick={() => onAction(service, 'stop')}
            >
              {pendingAction === 'stop' ? (
                <LoaderCircle className="spin" size={15} />
              ) : (
                <Square size={14} />
              )}
              停止服务
            </button>
          ) : (
            <button
              className="action-button start"
              type="button"
              disabled={busy || !canStart}
              onClick={() => onAction(service, 'start')}
            >
              {pendingAction === 'start' || service.status === 'starting' ? (
                <LoaderCircle className="spin" size={15} />
              ) : (
                <Play size={15} />
              )}
              {service.status === 'unavailable' ? '启动依赖并运行' : '启动服务'}
            </button>
          )}
        </div>
      </div>
    </article>
  )
}

function ServiceIcon({ service }: { service: ManagedService }) {
  if (service.scope === 'client') return <Zap size={20} />
  if (service.scope === 'control_plane') return <Layers3 size={20} />
  return <Server size={20} />
}

function StatusBadge({ status }: { status: ServiceStatus }) {
  const active = status === 'starting' || status === 'running' || status === 'stopping'
  return (
    <span className={'status-badge ' + status}>
      {active ? (
        <LoaderCircle className={status === 'running' ? '' : 'spin'} size={14} />
      ) : status === 'failed' ? (
        <AlertTriangle size={14} />
      ) : status === 'unavailable' ? (
        <Link2Off size={14} />
      ) : status === 'on_demand' ? (
        <Zap size={14} />
      ) : (
        <CircleStop size={14} />
      )}
      {serviceStatusLabels[status]}
    </span>
  )
}

function Detail({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="detail-item">
      <dt>{label}</dt>
      <dd>{children}</dd>
    </div>
  )
}

function Notice({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="notice error" role="alert">
      <AlertTriangle size={16} />
      <div>
        <strong>{title}</strong>
        <span>{children}</span>
      </div>
    </div>
  )
}

function EmptyState({
  icon,
  title,
  description,
}: {
  icon: ReactNode
  title: string
  description: string
}) {
  return (
    <div className="empty-state">
      <div>{icon}</div>
      <strong>{title}</strong>
      <p>{description}</p>
    </div>
  )
}

function lifecycleDescription(service: ManagedService): string {
  if (service.status === 'on_demand') return '客户端连接时按需启动'
  if (service.scope === 'control_plane' && service.status === 'unavailable') {
    return '操作时自动启动 Control Plane'
  }
  return '操作绑定 epoch ' + service.epoch
}

function formatOptionalDateTime(value: string | null): string {
  if (!value) return '—'
  const timestamp = Date.parse(value)
  return Number.isNaN(timestamp) ? value : formatDateTime(timestamp)
}

function formatDateTime(timestamp: number): string {
  return dateTimeFormatter.format(new Date(timestamp))
}

export default App
