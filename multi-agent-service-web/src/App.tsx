import { useEffect, useState } from 'react'
import type { FormEvent, ReactNode } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Activity,
  AlertTriangle,
  Boxes,
  CircleStop,
  Clock3,
  ExternalLink,
  FolderOpen,
  FolderLock,
  Gauge,
  Layers3,
  Link2Off,
  LoaderCircle,
  Play,
  Plus,
  RefreshCw,
  Save,
  Server,
  Settings2,
  ShieldCheck,
  Square,
  SquareTerminal,
  Trash2,
  Unplug,
  Wifi,
  WifiOff,
  Zap,
} from 'lucide-react'
import { api } from './api'
import type {
  LaunchMode,
  ManagedService,
  ManagementConfiguration,
  ManagementConfigurationUpdate,
  ProviderConfiguration,
  ProviderKind,
  ClaudeRuntimeMode,
  CoordinatorReasoningEffort,
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
  const configurationMutation = useMutation({
    mutationFn: api.updateConfiguration,
    onSuccess: (updated) => queryClient.setQueryData(['configuration'], updated),
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: ['configuration'] })
      void queryClient.invalidateQueries({ queryKey: ['services'] })
    },
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
  const controlPlane = services.find((service) => service.service_id === 'control-plane')
  const coordinator = services.find(
    (service) => service.service_id === 'multi-agent-coordinator',
  )
  const configurableServices = [controlPlane, coordinator]
  const configurationLocked = configurableServices.some(
    (service) => service === undefined || !['stopped', 'failed'].includes(service.status),
  )
  const configurationBlockingService = configurableServices.find(
    (service) => service !== undefined && !['stopped', 'failed'].includes(service.status),
  )
  const busy =
    serviceMutation.isPending || groupMutation.isPending || configurationMutation.isPending
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
    configurationMutation.reset()
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
            <span className="profile-pill">
              {configuration.providers.length} PROVIDER
              {configuration.providers.length === 1 ? '' : 'S'}
            </span>
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

      <ConfigurationPanel
        configuration={configuration}
        loading={configurationQuery.isLoading}
        locked={configurationLocked}
        blockingService={configurationBlockingService}
        saving={configurationMutation.isPending}
        loadError={configurationQuery.error?.message}
        saveError={configurationMutation.error?.message}
        onSave={(value) => {
          serviceMutation.reset()
          groupMutation.reset()
          configurationMutation.mutate(value)
        }}
      />

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

type ProviderDraft = {
  draftId: string
  providerId: string
  kind: ProviderKind
  codexHome: string
  configOverrides: string
  claudeConfigDir: string
  claudeCliPath: string
  modelIds: string
  networkDenyEnforced: boolean
}

type ConfigurationDraft = {
  providers: ProviderDraft[]
  allowedPathRoots: string
  claudeRuntimeMode: ClaudeRuntimeMode
  claudeOpencodexBaseUrl: string
  claudeOpencodexAuthTokenEnv: string
  coordinatorModel: string
  coordinatorReasoningEffort: CoordinatorReasoningEffort
  coordinatorApiKeyEnv: string
  coordinatorBaseUrl: string
  coordinatorMaxDecisionSteps: number
  coordinatorWaitTimeoutMs: number
  coordinatorMaxConcurrentDelegations: number
  coordinatorMaxTotalDelegations: number
  coordinatorMaxDelegationDepth: number
  coordinatorMaxPlanRevisions: number
  coordinatorMaxRetriesPerNode: number
  coordinatorMaxRuntimeMinutes: number
  coordinatorMaxModelActivations: number
}

let providerDraftSequence = 0

function createProviderDraft(
  kind: ProviderKind,
  configuration?: ProviderConfiguration,
  suggestedProviderId?: string,
): ProviderDraft {
  providerDraftSequence += 1
  return {
    draftId: 'provider-draft-' + providerDraftSequence,
    providerId:
      configuration?.provider_id ??
      suggestedProviderId ??
      (kind === 'fake' ? 'fake' : kind),
    kind,
    codexHome: configuration?.codex_home ?? '',
    configOverrides: configuration?.config_overrides.join('\n') ?? '',
    claudeConfigDir: configuration?.claude_config_dir ?? '',
    claudeCliPath: configuration?.claude_cli_path ?? '',
    modelIds: configuration?.model_ids.join('\n') ?? '',
    networkDenyEnforced: configuration?.network_deny_enforced ?? false,
  }
}

const emptyConfigurationDraft: ConfigurationDraft = {
  providers: [createProviderDraft('fake')],
  allowedPathRoots: '',
  claudeRuntimeMode: 'native',
  claudeOpencodexBaseUrl: 'http://127.0.0.1:10100',
  claudeOpencodexAuthTokenEnv: 'ANTHROPIC_AUTH_TOKEN',
  coordinatorModel: 'pixel/gpt-5.6-luna',
  coordinatorReasoningEffort: 'medium',
  coordinatorApiKeyEnv: 'OPENAI_API_KEY',
  coordinatorBaseUrl: 'http://127.0.0.1:10100/v1',
  coordinatorMaxDecisionSteps: 16,
  coordinatorWaitTimeoutMs: 0,
  coordinatorMaxConcurrentDelegations: 8,
  coordinatorMaxTotalDelegations: 30,
  coordinatorMaxDelegationDepth: 3,
  coordinatorMaxPlanRevisions: 10,
  coordinatorMaxRetriesPerNode: 2,
  coordinatorMaxRuntimeMinutes: 120,
  coordinatorMaxModelActivations: 50,
}

function ConfigurationPanel({
  configuration,
  loading,
  locked,
  blockingService,
  saving,
  loadError,
  saveError,
  onSave,
}: {
  configuration?: ManagementConfiguration
  loading: boolean
  locked: boolean
  blockingService?: ManagedService
  saving: boolean
  loadError?: string
  saveError?: string
  onSave: (value: ManagementConfigurationUpdate) => void
}) {
  const [draft, setDraft] = useState<ConfigurationDraft>(emptyConfigurationDraft)
  const directoryPickerMutation = useMutation({
    mutationFn: api.selectDirectory,
    onSuccess: ({ path }) => {
      if (path) {
        setDraft((current) => ({
          ...current,
          allowedPathRoots: appendConfigurationPath(current.allowedPathRoots, path),
        }))
      }
    },
  })

  useEffect(() => {
    if (configuration) {
      setDraft({
        providers: configuration.providers.map((provider) =>
          createProviderDraft(provider.kind, provider),
        ),
        allowedPathRoots: configuration.allowed_path_roots.join('\n'),
        claudeRuntimeMode: configuration.claude_runtime_mode,
        claudeOpencodexBaseUrl: configuration.claude_opencodex_base_url,
        claudeOpencodexAuthTokenEnv: configuration.claude_opencodex_auth_token_env,
        coordinatorModel: configuration.coordinator_model,
        coordinatorReasoningEffort: configuration.coordinator_reasoning_effort,
        coordinatorApiKeyEnv: configuration.coordinator_api_key_env,
        coordinatorBaseUrl: configuration.coordinator_base_url ?? '',
        coordinatorMaxDecisionSteps: configuration.coordinator_max_decision_steps,
        coordinatorWaitTimeoutMs: configuration.coordinator_wait_timeout_ms,
        coordinatorMaxConcurrentDelegations:
          configuration.coordinator_max_concurrent_delegations,
        coordinatorMaxTotalDelegations: configuration.coordinator_max_total_delegations,
        coordinatorMaxDelegationDepth: configuration.coordinator_max_delegation_depth,
        coordinatorMaxPlanRevisions: configuration.coordinator_max_plan_revisions,
        coordinatorMaxRetriesPerNode: configuration.coordinator_max_retries_per_node,
        coordinatorMaxRuntimeMinutes: configuration.coordinator_max_runtime_minutes,
        coordinatorMaxModelActivations: configuration.coordinator_max_model_activations,
      })
    }
  }, [configuration])

  const update = configurationUpdate(draft)
  const dirty = configuration ? !sameConfiguration(configuration, update) : false
  const validationError = configurationValidationError(update)
  const disabled = loading || locked || saving || configuration === undefined
  const pickerDisabled = disabled || directoryPickerMutation.isPending

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!disabled && dirty && validationError === null) {
      onSave(update)
    }
  }

  function updateProvider(
    draftId: string,
    transform: (provider: ProviderDraft) => ProviderDraft,
  ) {
    setDraft((current) => ({
      ...current,
      providers: current.providers.map((provider) =>
        provider.draftId === draftId ? transform(provider) : provider,
      ),
    }))
  }

  function changeProviderKind(draftId: string, kind: ProviderKind) {
    updateProvider(draftId, (provider) =>
      kind === 'fake'
        ? {
            ...provider,
            kind,
            codexHome: '',
            configOverrides: '',
            claudeConfigDir: '',
            claudeCliPath: '',
            modelIds: '',
            networkDenyEnforced: false,
          }
        : kind === 'codex'
          ? {
              ...provider,
              kind,
              claudeConfigDir: '',
              claudeCliPath: '',
              modelIds: '',
            }
          : {
              ...provider,
              kind,
              codexHome: '',
              configOverrides: '',
            },
    )
  }

  function removeProvider(draftId: string) {
    setDraft((current) => ({
      ...current,
      providers: current.providers.filter((provider) => provider.draftId !== draftId),
    }))
  }

  function addProvider() {
    setDraft((current) => ({
      ...current,
      providers: [
        ...current.providers,
        createProviderDraft('codex', undefined, nextProviderId(current.providers, 'codex')),
      ],
    }))
  }

  function chooseDirectory() {
    directoryPickerMutation.reset()
    directoryPickerMutation.mutate(firstConfigurationPath(draft.allowedPathRoots))
  }

  return (
    <section className="configuration-surface" aria-labelledby="configuration-heading">
      <div className="configuration-header">
        <div>
          <span className="section-kicker">CONTROL PLANE CONFIGURATION</span>
          <h2 id="configuration-heading">运行配置与路径筛选</h2>
          <p>
            单个 Control Plane 可注册多个 Provider；保存后在下一次启动时统一加载并按任务路由。
          </p>
        </div>
        <span className={'configuration-state ' + (locked ? 'locked' : 'editable')}>
          {locked ? <FolderLock size={14} /> : <Settings2 size={14} />}
          {locked ? '运行中 · 只读' : '已停止 · 可编辑'}
        </span>
      </div>

      {loadError ? (
        <Notice title="无法读取运行配置">{loadError}</Notice>
      ) : (
        <form className="configuration-form" onSubmit={submit}>
          <fieldset disabled={disabled}>
            <div className="runtime-backend-editor configuration-wide">
              <div className="provider-editor-header">
                <div>
                  <span>Claude 运行后端</span>
                  <small>
                    这是当前 Control Plane 的全局 Claude 路由。单个 Control Plane 不混用原生和
                    OpenCodex 环境；切换后需要重新启动核心服务。
                  </small>
                </div>
              </div>
              <div className="provider-card-grid">
                <label className="configuration-field">
                  <span>连接方式</span>
                  <select
                    value={draft.claudeRuntimeMode}
                    onChange={(event) =>
                      setDraft((current) => ({
                        ...current,
                        claudeRuntimeMode: event.target.value as ClaudeRuntimeMode,
                      }))
                    }
                  >
                    <option value="native">原生 Claude / Anthropic</option>
                    <option value="opencodex">OpenCodex 代理</option>
                  </select>
                </label>
                {draft.claudeRuntimeMode === 'opencodex' && (
                  <>
                    <label className="configuration-field">
                      <span>OpenCodex Base URL</span>
                      <input
                        value={draft.claudeOpencodexBaseUrl}
                        onChange={(event) =>
                          setDraft((current) => ({
                            ...current,
                            claudeOpencodexBaseUrl: event.target.value,
                          }))
                        }
                        placeholder="http://127.0.0.1:10100"
                        required
                      />
                    </label>
                    <label className="configuration-field">
                      <span>令牌环境变量名</span>
                      <input
                        value={draft.claudeOpencodexAuthTokenEnv}
                        onChange={(event) =>
                          setDraft((current) => ({
                            ...current,
                            claudeOpencodexAuthTokenEnv: event.target.value,
                          }))
                        }
                        placeholder="ANTHROPIC_AUTH_TOKEN"
                        required
                      />
                      <small>只保存变量名，不保存令牌；默认本机网关自动使用 opencodex-proxy，自定义网关需由宿主环境提供令牌。</small>
                    </label>
                    <div className="provider-fake-note provider-wide">
                      <Zap size={16} />
                      <span>
                        启动时会注入模型发现、Host 管理和自动压缩设置；模型 ID 请填写 OpenCodex
                        路由，例如 AIXW/gpt-5.6-sol。
                      </span>
                    </div>
                  </>
                )}
              </div>
            </div>

            <div className="runtime-backend-editor configuration-wide">
              <div className="provider-editor-header">
                <div>
                  <span>Coordinator Agent</span>
                  <small>
                    Microsoft Agent Framework 只负责计划、持续对话和调用 V3 工具。密钥仍由宿主环境提供，
                    不会写入运行配置。
                  </small>
                </div>
              </div>
              <div className="provider-card-grid">
                <label className="configuration-field">
                  <span>模型</span>
                  <input
                    value={draft.coordinatorModel}
                    onChange={(event) =>
                      setDraft((current) => ({
                        ...current,
                        coordinatorModel: event.target.value,
                      }))
                    }
                    placeholder="pixel/gpt-5.6-luna"
                    required
                  />
                </label>
                <label className="configuration-field">
                  <span>Reasoning Effort</span>
                  <select
                    value={draft.coordinatorReasoningEffort}
                    onChange={(event) =>
                      setDraft((current) => ({
                        ...current,
                        coordinatorReasoningEffort: event.target
                          .value as CoordinatorReasoningEffort,
                      }))
                    }
                  >
                    <option value="none">none</option>
                    <option value="low">low</option>
                    <option value="medium">medium</option>
                    <option value="high">high</option>
                    <option value="xhigh">xhigh</option>
                  </select>
                </label>
                <label className="configuration-field">
                  <span>OpenAI-compatible Base URL（可选）</span>
                  <input
                    value={draft.coordinatorBaseUrl}
                    onChange={(event) =>
                      setDraft((current) => ({
                        ...current,
                        coordinatorBaseUrl: event.target.value,
                      }))
                    }
                    placeholder="http://127.0.0.1:10100/v1"
                  />
                </label>
                <label className="configuration-field">
                  <span>API Key 环境变量名</span>
                  <input
                    value={draft.coordinatorApiKeyEnv}
                    onChange={(event) =>
                      setDraft((current) => ({
                        ...current,
                        coordinatorApiKeyEnv: event.target.value,
                      }))
                    }
                    placeholder="OPENAI_API_KEY"
                    required
                  />
                </label>
                <label className="configuration-field">
                  <span>单次激活最大决策步数</span>
                  <input
                    type="number"
                    min={1}
                    max={128}
                    value={draft.coordinatorMaxDecisionSteps}
                    onChange={(event) =>
                      setDraft((current) => ({
                        ...current,
                        coordinatorMaxDecisionSteps: Number(event.target.value),
                      }))
                    }
                    required
                  />
                </label>
                <label className="configuration-field">
                  <span>委派等待超时（毫秒）</span>
                  <input
                    type="number"
                    min={0}
                    max={300000}
                    value={draft.coordinatorWaitTimeoutMs}
                    onChange={(event) =>
                      setDraft((current) => ({
                        ...current,
                        coordinatorWaitTimeoutMs: Number(event.target.value),
                      }))
                    }
                    required
                  />
                </label>
                <label className="configuration-field">
                  <span>最大并行委派数</span>
                  <input
                    type="number"
                    min={1}
                    value={draft.coordinatorMaxConcurrentDelegations}
                    onChange={(event) =>
                      setDraft((current) => ({
                        ...current,
                        coordinatorMaxConcurrentDelegations: Number(event.target.value),
                      }))
                    }
                    required
                  />
                </label>
                <label className="configuration-field">
                  <span>最大委派总数</span>
                  <input
                    type="number"
                    min={1}
                    value={draft.coordinatorMaxTotalDelegations}
                    onChange={(event) =>
                      setDraft((current) => ({
                        ...current,
                        coordinatorMaxTotalDelegations: Number(event.target.value),
                      }))
                    }
                    required
                  />
                </label>
                <label className="configuration-field">
                  <span>最大子委派深度</span>
                  <input
                    type="number"
                    min={0}
                    value={draft.coordinatorMaxDelegationDepth}
                    onChange={(event) =>
                      setDraft((current) => ({
                        ...current,
                        coordinatorMaxDelegationDepth: Number(event.target.value),
                      }))
                    }
                    required
                  />
                </label>
                <label className="configuration-field">
                  <span>最大计划修订次数</span>
                  <input
                    type="number"
                    min={1}
                    value={draft.coordinatorMaxPlanRevisions}
                    onChange={(event) =>
                      setDraft((current) => ({
                        ...current,
                        coordinatorMaxPlanRevisions: Number(event.target.value),
                      }))
                    }
                    required
                  />
                </label>
                <label className="configuration-field">
                  <span>单节点最大重试次数</span>
                  <input
                    type="number"
                    min={0}
                    value={draft.coordinatorMaxRetriesPerNode}
                    onChange={(event) =>
                      setDraft((current) => ({
                        ...current,
                        coordinatorMaxRetriesPerNode: Number(event.target.value),
                      }))
                    }
                    required
                  />
                </label>
                <label className="configuration-field">
                  <span>最长运行时间（分钟）</span>
                  <input
                    type="number"
                    min={1}
                    value={draft.coordinatorMaxRuntimeMinutes}
                    onChange={(event) =>
                      setDraft((current) => ({
                        ...current,
                        coordinatorMaxRuntimeMinutes: Number(event.target.value),
                      }))
                    }
                    required
                  />
                </label>
                <label className="configuration-field">
                  <span>最大模型激活次数</span>
                  <input
                    type="number"
                    min={1}
                    value={draft.coordinatorMaxModelActivations}
                    onChange={(event) =>
                      setDraft((current) => ({
                        ...current,
                        coordinatorMaxModelActivations: Number(event.target.value),
                      }))
                    }
                    required
                  />
                </label>
                <div className="provider-fake-note provider-wide">
                  <Zap size={16} />
                  <span>
                    默认本机 OpenCodex 地址会使用固定令牌 opencodex-proxy；自定义地址、变量名或官方
                    OpenAI 请在启动统一平台前设置对应环境变量。0 毫秒等待表示由事件触发后续激活。
                  </span>
                </div>
              </div>
            </div>

            <div className="provider-editor configuration-wide">
              <div className="provider-editor-header">
                <div>
                  <span>Provider 实例</span>
                  <small>
                    同一后端的不同模型无需重复配置 Provider；不同 endpoint、账号或 Codex Home
                    才需要独立实例。
                  </small>
                </div>
                <button className="provider-add" type="button" onClick={addProvider}>
                  <Plus size={14} /> 添加 Provider
                </button>
              </div>

              <div className="provider-list">
                {draft.providers.map((provider, index) => (
                  <article className="provider-card" key={provider.draftId}>
                    <div className="provider-card-header">
                      <div>
                        <span className="provider-order">#{index + 1}</span>
                        <strong>{provider.providerId.trim() || '未命名 Provider'}</strong>
                        <span className={'provider-kind ' + provider.kind}>
                          {provider.kind.toUpperCase()}
                        </span>
                      </div>
                      <button
                        className="provider-remove"
                        type="button"
                        disabled={draft.providers.length === 1}
                        onClick={() => removeProvider(provider.draftId)}
                        title={
                          draft.providers.length === 1
                            ? '至少保留一个 Provider'
                            : '移除此 Provider'
                        }
                      >
                        <Trash2 size={14} /> 移除
                      </button>
                    </div>

                    <div className="provider-card-grid">
                      <label className="configuration-field">
                        <span>类型</span>
                        <select
                          value={provider.kind}
                          onChange={(event) =>
                            changeProviderKind(
                              provider.draftId,
                              event.target.value as ProviderKind,
                            )
                          }
                        >
                          <option value="fake">Fake · 应用层验收</option>
                          <option value="codex">Codex · 真实执行</option>
                          <option value="claude">Claude · Agent SDK</option>
                        </select>
                      </label>

                      <label className="configuration-field">
                        <span>Provider ID</span>
                        <input
                          value={provider.providerId}
                          onChange={(event) =>
                            updateProvider(provider.draftId, (current) => ({
                              ...current,
                              providerId: event.target.value,
                            }))
                          }
                          placeholder="codex"
                          required
                        />
                      </label>

                      {provider.kind === 'codex' ? (
                        <>
                          <label className="configuration-field provider-wide">
                            <span>Codex Home</span>
                            <input
                              value={provider.codexHome}
                              onChange={(event) =>
                                updateProvider(provider.draftId, (current) => ({
                                  ...current,
                                  codexHome: event.target.value,
                                }))
                              }
                              placeholder="C:/Users/<user>/.codex"
                              required
                            />
                            <small>必须是服务主机上已存在的绝对目录。</small>
                          </label>

                          <label className="configuration-field provider-wide">
                            <span>Codex 配置覆盖</span>
                            <textarea
                              value={provider.configOverrides}
                              onChange={(event) =>
                                updateProvider(provider.draftId, (current) => ({
                                  ...current,
                                  configOverrides: event.target.value,
                                }))
                              }
                              placeholder={
                                'model_provider="deepseek"\n' +
                                'model_providers.deepseek.env_key="DEEPSEEK_API_KEY"'
                              }
                              rows={3}
                            />
                            <small>
                              每行一个配置覆盖，仅支持 Provider 选择、endpoint 和 env_key 等安全引用；
                              不得写入 API Key、Token 或密码。
                            </small>
                          </label>

                          <label className="configuration-toggle provider-wide">
                            <input
                              type="checkbox"
                              checked={provider.networkDenyEnforced}
                              onChange={(event) =>
                                updateProvider(provider.draftId, (current) => ({
                                  ...current,
                                  networkDenyEnforced: event.target.checked,
                                }))
                              }
                            />
                            <span>
                              <strong>宿主已强制网络 deny</strong>
                              <small>
                                只有运行环境确实实施 deny 时才开启；这不是仅靠 UI 声明的策略。
                              </small>
                            </span>
                          </label>
                        </>
                      ) : provider.kind === 'claude' ? (
                        <>
                          <label className="configuration-field provider-wide">
                            <span>Claude 配置目录（可选）</span>
                            <input
                              value={provider.claudeConfigDir}
                              onChange={(event) =>
                                updateProvider(provider.draftId, (current) => ({
                                  ...current,
                                  claudeConfigDir: event.target.value,
                                }))
                              }
                              placeholder="C:/Users/<user>/.claude"
                            />
                            <small>留空使用 Claude CLI 默认配置目录；不得在此写入密钥。</small>
                          </label>

                          <label className="configuration-field provider-wide">
                            <span>Claude CLI 路径（可选）</span>
                            <input
                              value={provider.claudeCliPath}
                              onChange={(event) =>
                                updateProvider(provider.draftId, (current) => ({
                                  ...current,
                                  claudeCliPath: event.target.value,
                                }))
                              }
                              placeholder="留空由 SDK 自动发现原生 Claude CLI"
                            />
                          </label>

                          <label className="configuration-field provider-wide">
                            <span>Claude 模型 ID</span>
                            <textarea
                              value={provider.modelIds}
                              onChange={(event) =>
                                updateProvider(provider.draftId, (current) => ({
                                  ...current,
                                  modelIds: event.target.value,
                                }))
                              }
                              placeholder={
                                draft.claudeRuntimeMode === 'opencodex'
                                  ? 'AIXW/gpt-5.6-sol\npixel/gpt-5.6-sol'
                                  : 'claude-sonnet-4-5\nclaude-opus-4-5'
                              }
                              rows={2}
                              required
                            />
                            <small>
                              每行一个模型 ID；
                              {draft.claudeRuntimeMode === 'opencodex'
                                ? '请填写 OpenCodex 路由，例如 AIXW/gpt-5.6-sol。'
                                : '请填写 Claude 原生模型，例如 claude-sonnet-4-5。'}
                            </small>
                          </label>

                          <label className="configuration-toggle provider-wide">
                            <input
                              type="checkbox"
                              checked={provider.networkDenyEnforced}
                              onChange={(event) =>
                                updateProvider(provider.draftId, (current) => ({
                                  ...current,
                                  networkDenyEnforced: event.target.checked,
                                }))
                              }
                            />
                            <span>
                              <strong>宿主已强制网络 deny</strong>
                              <small>只有运行环境确实实施 deny 时才开启。</small>
                            </span>
                          </label>
                        </>
                      ) : (
                        <div className="provider-fake-note provider-wide">
                          <Boxes size={16} />
                          <span>Fake Provider 仅用于确定性的应用层验收，不调用真实模型。</span>
                        </div>
                      )}
                    </div>
                  </article>
                ))}
              </div>
            </div>

            <div className="configuration-field configuration-wide">
              <span>允许的工作目录根路径</span>
              <div className="configuration-path-editor">
                <textarea
                  aria-label="允许的工作目录根路径"
                  value={draft.allowedPathRoots}
                  onChange={(event) => {
                    directoryPickerMutation.reset()
                    setDraft((current) => ({
                      ...current,
                      allowedPathRoots: event.target.value,
                    }))
                  }}
                  placeholder={'D:/dev\nE:/projects/approved'}
                  rows={4}
                />
                <button
                  className="configuration-browse"
                  type="button"
                  onClick={chooseDirectory}
                  disabled={pickerDisabled}
                >
                  {directoryPickerMutation.isPending ? (
                    <LoaderCircle className="spin" size={15} />
                  ) : (
                    <FolderOpen size={15} />
                  )}
                  选择文件夹
                </button>
              </div>
              <small>
                每行一个已存在的绝对目录。可重复选择多个目录；选择窗口在运行 Management API 的本机打开。
                留空表示不筛选，MCP 可为每次委派传入任意存在目录。
              </small>
              {directoryPickerMutation.error && (
                <small className="configuration-picker-error" role="alert">
                  无法打开文件夹选择器：{directoryPickerMutation.error.message}
                </small>
              )}
            </div>

          </fieldset>

          <div className="configuration-footer">
            <div className={validationError !== null && !locked ? 'invalid' : ''}>
              {validationError !== null && !locked ? (
                <AlertTriangle size={15} />
              ) : (
                <FolderLock size={15} />
              )}
              <span>
                {locked
                  ? '请先在统一平台停止核心服务，再修改配置。阻塞服务：' +
                    (blockingService
                      ? blockingService.display_name +
                        '（' +
                        serviceStatusLabels[blockingService.status] +
                        '）'
                      : '状态同步中')
                  : validationError ??
                    '配置保存后不会自动启动服务，可检查后再点击“启动核心”。'}
              </span>
            </div>
            <button
              className="configuration-save"
              type="submit"
              disabled={disabled || !dirty || validationError !== null}
            >
              {saving ? <LoaderCircle className="spin" size={15} /> : <Save size={15} />}
              保存运行配置
            </button>
          </div>
        </form>
      )}

      {saveError && <Notice title="运行配置未保存">{saveError}</Notice>}
    </section>
  )
}

function configurationUpdate(draft: ConfigurationDraft): ManagementConfigurationUpdate {
  return {
    providers: draft.providers.map((provider) => ({
      provider_id: provider.providerId.trim(),
      kind: provider.kind,
      codex_home: provider.kind === 'codex' ? provider.codexHome.trim() || null : null,
      config_overrides:
        provider.kind === 'codex' ? nonEmptyLines(provider.configOverrides) : [],
      claude_config_dir:
        provider.kind === 'claude' ? provider.claudeConfigDir.trim() || null : null,
      claude_cli_path:
        provider.kind === 'claude' ? provider.claudeCliPath.trim() || null : null,
      model_ids: provider.kind === 'claude' ? nonEmptyLines(provider.modelIds) : [],
      network_deny_enforced:
        provider.kind !== 'fake' && provider.networkDenyEnforced,
    })),
    allowed_path_roots: nonEmptyLines(draft.allowedPathRoots),
    claude_runtime_mode: draft.claudeRuntimeMode,
    claude_opencodex_base_url: draft.claudeOpencodexBaseUrl.trim(),
    claude_opencodex_auth_token_env: draft.claudeOpencodexAuthTokenEnv.trim(),
    coordinator_model: draft.coordinatorModel.trim(),
    coordinator_reasoning_effort: draft.coordinatorReasoningEffort,
    coordinator_api_key_env: draft.coordinatorApiKeyEnv.trim(),
    coordinator_base_url: draft.coordinatorBaseUrl.trim() || null,
    coordinator_max_decision_steps: draft.coordinatorMaxDecisionSteps,
    coordinator_wait_timeout_ms: draft.coordinatorWaitTimeoutMs,
    coordinator_max_concurrent_delegations: draft.coordinatorMaxConcurrentDelegations,
    coordinator_max_total_delegations: draft.coordinatorMaxTotalDelegations,
    coordinator_max_delegation_depth: draft.coordinatorMaxDelegationDepth,
    coordinator_max_plan_revisions: draft.coordinatorMaxPlanRevisions,
    coordinator_max_retries_per_node: draft.coordinatorMaxRetriesPerNode,
    coordinator_max_runtime_minutes: draft.coordinatorMaxRuntimeMinutes,
    coordinator_max_model_activations: draft.coordinatorMaxModelActivations,
  }
}

function configurationValidationError(
  update: ManagementConfigurationUpdate,
): string | null {
  if (update.providers.length === 0) return '至少需要配置一个 Provider。'
  if (update.claude_opencodex_base_url.length === 0) {
    return 'Claude OpenCodex Base URL 不能为空。'
  }
  if (update.claude_opencodex_auth_token_env.length === 0) {
    return 'Claude OpenCodex 令牌环境变量名不能为空。'
  }
  if (update.coordinator_model.length === 0) return 'Coordinator 模型不能为空。'
  if (update.coordinator_api_key_env.length === 0) {
    return 'Coordinator API Key 环境变量名不能为空。'
  }
  if (
    !Number.isInteger(update.coordinator_max_decision_steps) ||
    update.coordinator_max_decision_steps < 1 ||
    update.coordinator_max_decision_steps > 128
  ) {
    return 'Coordinator 最大决策步数必须是 1 到 128 的整数。'
  }
  if (
    !Number.isInteger(update.coordinator_wait_timeout_ms) ||
    update.coordinator_wait_timeout_ms < 0 ||
    update.coordinator_wait_timeout_ms > 300000
  ) {
    return 'Coordinator 委派等待超时必须是 0 到 300000 的整数。'
  }
  const positiveAutonomyLimits: Array<[number, string]> = [
    [update.coordinator_max_concurrent_delegations, '最大并行委派数'],
    [update.coordinator_max_total_delegations, '最大委派总数'],
    [update.coordinator_max_plan_revisions, '最大计划修订次数'],
    [update.coordinator_max_runtime_minutes, '最长运行时间'],
    [update.coordinator_max_model_activations, '最大模型激活次数'],
  ]
  for (const [value, label] of positiveAutonomyLimits) {
    if (!Number.isInteger(value) || value < 1) return 'Coordinator ' + label + '必须是正整数。'
  }
  const nonNegativeAutonomyLimits: Array<[number, string]> = [
    [update.coordinator_max_delegation_depth, '最大子委派深度'],
    [update.coordinator_max_retries_per_node, '单节点最大重试次数'],
  ]
  for (const [value, label] of nonNegativeAutonomyLimits) {
    if (!Number.isInteger(value) || value < 0) {
      return 'Coordinator ' + label + '必须是非负整数。'
    }
  }
  const ids = new Set<string>()
  for (const [index, provider] of update.providers.entries()) {
    if (!provider.provider_id) return '第 ' + (index + 1) + ' 个 Provider ID 不能为空。'
    if (ids.has(provider.provider_id)) return 'Provider ID 必须唯一：' + provider.provider_id
    ids.add(provider.provider_id)
    if (provider.kind === 'codex' && provider.codex_home === null) {
      return 'Codex Provider ' + provider.provider_id + ' 必须填写 Codex Home。'
    }
    if (provider.kind === 'claude' && provider.model_ids.length === 0) {
      return 'Claude Provider ' + provider.provider_id + ' 至少需要一个模型 ID。'
    }
  }
  return null
}

function nonEmptyLines(value: string): string[] {
  return value
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean)
}

function nextProviderId(providers: ProviderDraft[], base: string): string {
  const providerIds = new Set(providers.map((provider) => provider.providerId.trim()))
  if (!providerIds.has(base)) return base
  let suffix = 2
  while (providerIds.has(base + '-' + suffix)) suffix += 1
  return base + '-' + suffix
}

function firstConfigurationPath(value: string): string | null {
  return (
    value
      .split(/\r?\n/)
      .map((path) => path.trim())
      .find(Boolean) ?? null
  )
}

function appendConfigurationPath(current: string, selected: string): string {
  const paths = current
    .split(/\r?\n/)
    .map((path) => path.trim())
    .filter(Boolean)
  const normalizedSelected = selected.replaceAll('\\', '/')
  if (paths.some((path) => path.replaceAll('\\', '/') === normalizedSelected)) {
    return paths.join('\n')
  }
  return [...paths, selected].join('\n')
}

function sameConfiguration(
  current: ManagementConfiguration,
  update: ManagementConfigurationUpdate,
): boolean {
  return (
    current.providers.length === update.providers.length &&
    current.providers.every((provider, index) =>
      sameProviderConfiguration(provider, update.providers[index]),
    ) &&
    current.allowed_path_roots.length === update.allowed_path_roots.length &&
    current.allowed_path_roots.every((path, index) => path === update.allowed_path_roots[index]) &&
    current.claude_runtime_mode === update.claude_runtime_mode &&
    current.claude_opencodex_base_url === update.claude_opencodex_base_url &&
    current.claude_opencodex_auth_token_env === update.claude_opencodex_auth_token_env &&
    current.coordinator_model === update.coordinator_model &&
    current.coordinator_reasoning_effort === update.coordinator_reasoning_effort &&
    current.coordinator_api_key_env === update.coordinator_api_key_env &&
    current.coordinator_base_url === update.coordinator_base_url &&
    current.coordinator_max_decision_steps === update.coordinator_max_decision_steps &&
    current.coordinator_wait_timeout_ms === update.coordinator_wait_timeout_ms &&
    current.coordinator_max_concurrent_delegations ===
      update.coordinator_max_concurrent_delegations &&
    current.coordinator_max_total_delegations === update.coordinator_max_total_delegations &&
    current.coordinator_max_delegation_depth === update.coordinator_max_delegation_depth &&
    current.coordinator_max_plan_revisions === update.coordinator_max_plan_revisions &&
    current.coordinator_max_retries_per_node === update.coordinator_max_retries_per_node &&
    current.coordinator_max_runtime_minutes === update.coordinator_max_runtime_minutes &&
    current.coordinator_max_model_activations === update.coordinator_max_model_activations
  )
}

function sameProviderConfiguration(
  current: ProviderConfiguration,
  update: ProviderConfiguration | undefined,
): boolean {
  return (
    update !== undefined &&
    current.provider_id === update.provider_id &&
    current.kind === update.kind &&
    current.codex_home === update.codex_home &&
    current.claude_config_dir === update.claude_config_dir &&
    current.claude_cli_path === update.claude_cli_path &&
    current.network_deny_enforced === update.network_deny_enforced &&
    current.model_ids.length === update.model_ids.length &&
    current.model_ids.every((model, index) => model === update.model_ids[index]) &&
    current.config_overrides.length === update.config_overrides.length &&
    current.config_overrides.every(
      (override, index) => override === update.config_overrides[index],
    )
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
