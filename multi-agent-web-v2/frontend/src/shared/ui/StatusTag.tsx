import { Tag } from "antd";

const color: Record<string, string> = {
  pending_start: "default",
  running: "processing",
  waiting: "cyan",
  waiting_approval: "gold",
  waiting_event: "cyan",
  succeeded: "success",
  failed: "error",
  timed_out: "error",
  cancelled: "default",
  attention_required: "magenta",
  reconciliation_required: "magenta",
  approved: "success",
  rejected: "error",
  pending: "gold",
};

const label: Record<string, string> = {
  pending_start: "等待启动",
  running: "运行中",
  waiting: "等待中",
  waiting_approval: "等待审批",
  waiting_event: "等待事件",
  succeeded: "成功",
  failed: "失败",
  timed_out: "超时",
  cancelled: "已取消",
  attention_required: "需要处理",
  reconciliation_required: "需要对账",
  approved: "已批准",
  rejected: "已拒绝",
  pending: "待处理",
};

export function StatusTag({ status }: { status: string }) {
  return (
    <Tag color={color[status] ?? "default"} bordered={false}>
      {label[status] ?? status}
    </Tag>
  );
}
