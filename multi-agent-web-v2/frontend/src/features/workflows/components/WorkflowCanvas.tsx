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
import { Empty, Tag, Typography } from "antd";
import { memo, useMemo } from "react";
import type { NodeProjection, WorkflowDocument, WorkflowNode } from "../../../shared/types";

interface FlowData extends Record<string, unknown> {
  node: WorkflowNode;
  status?: string;
}

type FlowNode = Node<FlowData, "workflow-node">;

const statusColor: Record<string, string> = {
  running: "processing",
  waiting_approval: "warning",
  waiting_event: "cyan",
  succeeded: "success",
  failed: "error",
  timed_out: "error",
  cancelled: "default",
  reconciliation_required: "magenta",
};

const WorkflowNodeCard = memo(function WorkflowNodeCard({
  data,
  selected,
}: NodeProps<FlowNode>) {
  return (
    <div className={`workflow-node ${selected ? "selected" : ""} ${data.status ?? "draft"}`}>
      <Handle type="target" position={Position.Left} />
      <div className="workflow-node__head">
        <span className={`node-kind node-kind--${data.node.type}`}>{data.node.type[0]}</span>
        <div>
          <Typography.Text strong>{data.node.id}</Typography.Text>
          <Typography.Text type="secondary">{data.node.type}</Typography.Text>
        </div>
        {data.status && (
          <Tag color={statusColor[data.status] ?? "default"} bordered={false}>
            {data.status}
          </Tag>
        )}
      </div>
      {data.node.agent && (
        <div className="workflow-node__meta">
          <span>{data.node.agent.model || "未选择模型"}</span>
          <span>{data.node.agent.effort || "未选择 effort"}</span>
        </div>
      )}
      <Handle type="source" position={Position.Right} />
    </div>
  );
});

const nodeTypes = { "workflow-node": WorkflowNodeCard };

function layout(document: WorkflowDocument): Map<string, { x: number; y: number }> {
  const levels = new Map<string, number>();
  const incoming = new Map<string, string[]>();
  document.spec.nodes.forEach((node) => incoming.set(node.id, []));
  document.spec.transitions.forEach((edge) => incoming.get(edge.to)?.push(edge.from));
  const visit = (nodeId: string, stack = new Set<string>()): number => {
    const cached = levels.get(nodeId);
    if (cached !== undefined) return cached;
    if (stack.has(nodeId)) return 0;
    stack.add(nodeId);
    const parents = incoming.get(nodeId) ?? [];
    const level = parents.length ? Math.max(...parents.map((parent) => visit(parent, stack))) + 1 : 0;
    stack.delete(nodeId);
    levels.set(nodeId, level);
    return level;
  };
  document.spec.nodes.forEach((node) => visit(node.id));
  const groups = new Map<number, WorkflowNode[]>();
  document.spec.nodes.forEach((node) => {
    const level = levels.get(node.id) ?? 0;
    groups.set(level, [...(groups.get(level) ?? []), node]);
  });
  const positions = new Map<string, { x: number; y: number }>();
  groups.forEach((nodes, level) =>
    nodes.forEach((node, index) =>
      positions.set(node.id, {
        x: level * 330 + 80,
        y: index * 160 + 100 - ((nodes.length - 1) * 160) / 2,
      }),
    ),
  );
  return positions;
}

interface Props {
  document: WorkflowDocument;
  selectedNodeId?: string | null;
  projections?: NodeProjection[];
  onSelectNode?: (nodeId: string) => void;
}

export function WorkflowCanvas({
  document,
  selectedNodeId,
  projections = [],
  onSelectNode,
}: Props) {
  const positions = useMemo(() => layout(document), [document]);
  const status = useMemo(
    () => new Map(projections.map((projection) => [projection.nodeId, projection.status])),
    [projections],
  );
  const nodes = useMemo<FlowNode[]>(
    () =>
      document.spec.nodes.map((node) => ({
        id: node.id,
        type: "workflow-node",
        position: positions.get(node.id) ?? { x: 80, y: 100 },
        selected: node.id === selectedNodeId,
        draggable: false,
        data: { node, status: status.get(node.id) },
      })),
    [document.spec.nodes, positions, selectedNodeId, status],
  );
  const edges = useMemo<Edge[]>(
    () =>
      document.spec.transitions.map((transition) => ({
        id: transition.id,
        source: transition.from,
        target: transition.to,
        label: transition.on,
        animated: status.get(transition.to) === "running",
        className: `workflow-edge workflow-edge--${status.get(transition.to) ?? "draft"}`,
      })),
    [document.spec.transitions, status],
  );

  if (!nodes.length) {
    return (
      <div className="canvas-empty">
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description={
            <span>
              尚未添加节点
              <small>使用工具栏中的“添加节点”，任务参数会在右侧抽屉编辑。</small>
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
      fitViewOptions={{ padding: 0.3, maxZoom: 1.1 }}
      minZoom={0.3}
      maxZoom={1.6}
      nodesConnectable={false}
      nodesDraggable={false}
      onNodeClick={(_, node) => onSelectNode?.(node.id)}
      proOptions={{ hideAttribution: true }}
    >
      <Background variant={BackgroundVariant.Dots} gap={22} size={1.2} color="#cbd5e1" />
      <Controls showInteractive={false} position="bottom-left" />
      <MiniMap position="bottom-right" pannable zoomable />
    </ReactFlow>
  );
}
