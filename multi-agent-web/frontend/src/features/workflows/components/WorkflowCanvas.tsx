import { DeleteOutlined, SettingOutlined } from "@ant-design/icons";
import {
  Background,
  BackgroundVariant,
  Controls,
  Handle,
  MiniMap,
  Position,
  ReactFlow,
  type Edge,
  type Node,
  type NodeProps,
} from "@xyflow/react";
import { Button, Empty, Tag, Tooltip, Typography } from "antd";
import { memo, useMemo } from "react";
import type { TaskDraft } from "../../../shared/types";

interface FlowNodeData extends Record<string, unknown> {
  task: TaskDraft;
  status?: string;
  onDelete?: (taskId: string) => void;
}

type TaskFlowNode = Node<FlowNodeData, "task">;

const statusColors: Record<string, string> = {
  pending: "default",
  ready: "cyan",
  queued: "geekblue",
  running: "processing",
  awaiting_approval: "warning",
  succeeded: "success",
  failed: "error",
  cancelled: "default",
  interrupted: "warning",
  blocked: "error",
};

const statusLabels: Record<string, string> = {
  pending: "等待",
  ready: "就绪",
  queued: "队列中",
  running: "执行中",
  awaiting_approval: "等待准入",
  succeeded: "成功",
  failed: "失败",
  cancelled: "已取消",
  interrupted: "已中断",
  blocked: "已阻塞",
};

const TaskNodeView = memo(function TaskNodeView({ data, selected }: NodeProps<TaskFlowNode>) {
  const { task, status, onDelete } = data;
  return (
    <div className={`flow-task-node ${selected ? "selected" : ""} ${status ?? "draft"}`}>
      <Handle type="target" position={Position.Left} className="flow-handle" />
      <div className="flow-task-head">
        <span className="flow-task-icon"><SettingOutlined /></span>
        <div>
          <Typography.Text strong ellipsis={{ tooltip: task.id }}>{task.id}</Typography.Text>
          <Typography.Text type="secondary">{task.role || "worker"}</Typography.Text>
        </div>
        {status ? (
          <Tag color={statusColors[status] ?? "default"} bordered={false}>
            {statusLabels[status] ?? status}
          </Tag>
        ) : (
          onDelete && (
            <Tooltip title="删除任务">
              <Button
                className="node-delete nodrag"
                type="text"
                size="small"
                icon={<DeleteOutlined />}
                onClick={(event) => {
                  event.stopPropagation();
                  onDelete(task.id);
                }}
              />
            </Tooltip>
          )
        )}
      </div>
      <div className="flow-task-meta">
        <span>{task.provider || "未选择 Provider"}</span>
        <span>{task.model || "未选择模型"}</span>
      </div>
      <div className="flow-task-foot">
        <span>{task.workspace_id || "未选择工作区"}</span>
        <span>{task.effort ? `推理 ${task.effort}` : "未选择推理等级"}</span>
      </div>
      <Handle type="source" position={Position.Right} className="flow-handle" />
    </div>
  );
});

const nodeTypes = { task: TaskNodeView };

function layoutTasks(tasks: TaskDraft[]): Map<string, { x: number; y: number }> {
  const known = new Map(tasks.map((task) => [task.id, task]));
  const cache = new Map<string, number>();
  const visiting = new Set<string>();

  const levelFor = (taskId: string): number => {
    if (cache.has(taskId)) return cache.get(taskId)!;
    if (visiting.has(taskId)) return 0;
    visiting.add(taskId);
    const task = known.get(taskId);
    const level = task?.depends_on.length
      ? Math.max(...task.depends_on.map((dependency) => levelFor(dependency))) + 1
      : 0;
    visiting.delete(taskId);
    cache.set(taskId, level);
    return level;
  };

  const groups = new Map<number, TaskDraft[]>();
  tasks.forEach((task) => {
    const level = levelFor(task.id);
    groups.set(level, [...(groups.get(level) ?? []), task]);
  });
  const positions = new Map<string, { x: number; y: number }>();
  groups.forEach((group, level) => {
    group.forEach((task, index) => {
      const offset = ((group.length - 1) * 156) / 2;
      positions.set(task.id, { x: level * 330 + 70, y: index * 156 - offset + 220 });
    });
  });
  return positions;
}

interface WorkflowCanvasProps {
  tasks: TaskDraft[];
  selectedTaskId?: string | null;
  statuses?: Record<string, string>;
  readOnly?: boolean;
  onSelectTask?: (taskId: string) => void;
  onDeleteTask?: (taskId: string) => void;
}

export function WorkflowCanvas({
  tasks,
  selectedTaskId,
  statuses = {},
  readOnly = false,
  onSelectTask,
  onDeleteTask,
}: WorkflowCanvasProps) {
  const positions = useMemo(() => layoutTasks(tasks), [tasks]);
  const nodes = useMemo<TaskFlowNode[]>(
    () =>
      tasks.map((task) => ({
        id: task.id,
        type: "task",
        position: positions.get(task.id) ?? { x: 80, y: 180 },
        selected: task.id === selectedTaskId,
        draggable: false,
        data: {
          task,
          status: statuses[task.id],
          onDelete: readOnly ? undefined : onDeleteTask,
        },
      })),
    [onDeleteTask, positions, readOnly, selectedTaskId, statuses, tasks],
  );
  const edges = useMemo<Edge[]>(
    () =>
      tasks.flatMap((task) =>
        task.depends_on.map((dependency) => ({
          id: `${dependency}->${task.id}`,
          source: dependency,
          target: task.id,
          animated: statuses[task.id] === "running",
          className: `flow-edge ${statuses[task.id] ?? "draft"}`,
        })),
      ),
    [statuses, tasks],
  );

  if (!tasks.length) {
    return (
      <div className="canvas-empty">
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description={
            <span>
              还没有任务节点
              <small>使用右上角“添加任务”创建工作流的第一个节点</small>
            </span>
          }
        />
      </div>
    );
  }

  return (
    <ReactFlow
      nodes={nodes}
      edges={edges}
      nodeTypes={nodeTypes}
      fitView
      fitViewOptions={{ padding: 0.25, maxZoom: 1.15 }}
      minZoom={0.35}
      maxZoom={1.6}
      nodesConnectable={false}
      nodesDraggable={false}
      elementsSelectable
      onNodeClick={(_, node) => onSelectTask?.(node.id)}
      proOptions={{ hideAttribution: true }}
    >
      <Background variant={BackgroundVariant.Dots} gap={22} size={1.2} color="#cbd5e1" />
      <Controls showInteractive={false} position="bottom-left" />
      <MiniMap
        pannable
        zoomable
        position="bottom-right"
        nodeColor={(node) => {
          const status = statuses[node.id];
          if (status === "succeeded") return "#12b76a";
          if (status === "failed" || status === "blocked") return "#f04438";
          if (status === "running") return "#4f46e5";
          return "#94a3b8";
        }}
      />
    </ReactFlow>
  );
}
