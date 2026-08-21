import { useMemo, type ReactNode } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Activity,
  AlertTriangle,
  CircleStop,
  Clock3,
  ExternalLink,
  Gauge,
  LoaderCircle,
  Play,
  RefreshCw,
  Server,
  ShieldCheck,
  Square,
  SquareTerminal,
  Wifi,
  WifiOff,
} from 'lucide-react'
import { api } from './api'
import type {
  ManagedService,
  ServiceAction,
  ServiceActionRequest,
  ServiceStatus,
} from './types'

const serviceStatusLabels: Record<ServiceStatus, string> = {
  stopped: '已停止',
  starting: '启动中',
  running: '运行中',
  stopping: '停止中',
  failed: '失败',
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
  const services = useMemo(
    () =>
      [...(servicesQuery.data ?? [])].sort((left, right) =>
        (left.category + '\u0000' + left.display_name).localeCompare(
          right.category + '\u0000' + right.display_name,
          'zh-CN',
        ),
      ),
    [servicesQuery.data],
  )
  const runningCount = services.filter((service) => service.status === 'running').length
  const transitioningCount = services.filter((service) =>
    ['starting', 'stopping'].includes(service.status),
  ).length
  const failedCount = services.filter((service) => service.status === 'failed').length
  const pendingRequest = serviceMutation.isPending ? serviceMutation.variables : null

  function runAction(service: ManagedService, action: ServiceAction) {
    serviceMutation.reset()
    serviceMutation.mutate({ service, action })
  }

  function refresh() {
    serviceMutation.reset()
    void servicesQuery.refetch()
  }

  return (
    <main className="shell">
      <header className="hero">
        <div>
          <div className="brand-line">
            <span className="brand-mark">
              <ShieldCheck size={18} />
            </span>
            <span>AITools / Service Runtime</span>
          </div>
          <h1>服务生命周期控制台</h1>
          <p>
            管理 Control Plane 已登记的本地服务。页面不接受命令、工作目录或环境变量，
            每次启停都使用当前 epoch 防止操作陈旧进程。
          </p>
        </div>
        <div className="hero-actions">
          <ConnectionState
            loading={servicesQuery.isLoading}
            error={servicesQuery.error?.message}
          />
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

      <section className="metric-grid" aria-label="服务状态概览">
        <Metric icon={<Server size={19} />} label="登记服务" value={services.length} />
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
          icon={<AlertTriangle size={19} />}
          label="需要关注"
          value={failedCount}
          tone="red"
        />
      </section>

      <section className="service-surface">
        <div className="surface-header">
          <div>
            <span className="section-kicker">REGISTERED SERVICES</span>
            <h2>当前服务目录</h2>
          </div>
          <div className="polling-note">
            <Gauge size={15} /> 每 2 秒同步一次
            {servicesQuery.dataUpdatedAt > 0 && (
              <span>· 最近更新 {formatDateTime(servicesQuery.dataUpdatedAt)}</span>
            )}
          </div>
        </div>

        {servicesQuery.error && (
          <Notice title="无法读取服务目录">{servicesQuery.error.message}</Notice>
        )}
        {serviceMutation.error && (
          <Notice title="服务操作未完成">
            {serviceMutation.error.message}。状态已重新同步，请按最新 epoch 重试。
          </Notice>
        )}

        {servicesQuery.isLoading ? (
          <EmptyState
            icon={<LoaderCircle className="spin" size={26} />}
            title="正在连接 Control Plane"
            description="服务目录就绪后会自动显示在这里。"
          />
        ) : services.length === 0 ? (
          <EmptyState
            icon={<Server size={26} />}
            title="当前没有可管理服务"
            description="请启动带静态服务目录的 Control Plane Profile。"
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
        <WifiOff size={15} /> 连接异常
      </div>
    )
  }
  return (
    <div className="connection-pill online">
      <Wifi size={15} /> Control Plane 已连接
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
  onAction,
}: {
  service: ManagedService
  pendingAction: ServiceAction | null
  onAction: (service: ManagedService, action: ServiceAction) => void
}) {
  const transitioning = service.status === 'starting' || service.status === 'stopping'
  const busy = pendingAction !== null || transitioning
  const canStart = service.controllable && ['stopped', 'failed'].includes(service.status)
  const canStop = service.controllable && service.status === 'running'

  return (
    <article className={'service-card status-' + service.status}>
      <div className="card-accent" />
      <div className="service-heading">
        <div className="service-identity">
          <div className="service-icon">
            <Server size={20} />
          </div>
          <div>
            <div className="category-row">
              <span className="category-tag">{service.category}</span>
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
            '—'
          )}
        </Detail>
        <Detail label="Process">
          {service.pid === null ? '未运行' : 'PID ' + service.pid}
        </Detail>
        <Detail label="Epoch">
          <span className="epoch-value">{service.epoch}</span>
        </Detail>
        <Detail label="最近启动">{formatOptionalDateTime(service.started_at)}</Detail>
        <Detail label="最近停止">{formatOptionalDateTime(service.stopped_at)}</Detail>
        <Detail label="退出码">{service.exit_code ?? '—'}</Detail>
      </dl>

      {service.last_error && <Notice title="最近错误">{service.last_error}</Notice>}

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
          <ShieldCheck size={14} /> 操作绑定 epoch {service.epoch}
        </span>
        <div className="service-actions">
          {!service.controllable ? (
            <span className="readonly-label">仅观察</span>
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
              启动服务
            </button>
          )}
        </div>
      </div>
    </article>
  )
}

function StatusBadge({ status }: { status: ServiceStatus }) {
  const active = status === 'starting' || status === 'running' || status === 'stopping'
  return (
    <span className={'status-badge ' + status}>
      {active ? (
        <LoaderCircle className={status === 'running' ? '' : 'spin'} size={14} />
      ) : status === 'failed' ? (
        <AlertTriangle size={14} />
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

function formatOptionalDateTime(value: string | null): string {
  if (!value) return '—'
  const timestamp = Date.parse(value)
  return Number.isNaN(timestamp) ? value : formatDateTime(timestamp)
}

function formatDateTime(timestamp: number): string {
  return dateTimeFormatter.format(new Date(timestamp))
}

export default App
