export const coordinatorStatusLabels: Record<string, string> = {
  accepted: '已验收',
  active: '执行中',
  awaiting_event: '等待事件',
  cancelled: '已取消',
  completed: '已完成',
  delegated: '已派遣',
  draft: '计划草稿',
  failed: '失败',
  paused: '已暂停',
  preparing: '准备中',
  proposed: '待规划',
  ready: '待派遣',
  reconciliation_required: '待对账',
  reconciling: '对账中',
  reporting: '整理结果',
  review_required: '待验收',
  reviewing: '待验收',
  running: '执行中',
  succeeded: '已完成',
  waiting: '等待事件',
  waiting_input: '等待输入',
}

export function CoordinatorStatus({ status }: { status: string }) {
  return (
    <span className={'coordinator-status ' + status}>
      <span />
      {coordinatorStatusLabels[status] ?? status}
    </span>
  )
}
