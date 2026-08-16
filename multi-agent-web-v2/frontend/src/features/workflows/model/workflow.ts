import type {
  CatalogModel,
  WorkflowDocument,
  WorkflowNode,
  WorkflowTransition,
} from "../../../shared/types";

const closedObject = (): Record<string, unknown> => ({
  type: "object",
  properties: {},
  required: [],
  additionalProperties: false,
});

export function createWorkflow(): WorkflowDocument {
  return {
    apiVersion: "orchestration.misaka.dev/v1",
    kind: "Workflow",
    metadata: {
      id: "new-workflow",
      version: 1,
      name: "未命名工作流",
      description: "通过 Multi-Agent Control Plane 创建",
    },
    spec: {
      flow: { type: "dag" },
      inputSchema: closedObject(),
      outputSchema: closedObject(),
      failurePolicy: "continue_independent",
      maxConcurrency: 4,
      nodes: [],
      transitions: [],
      outputs: [],
    },
  };
}

export function createAgentNode(
  id: string,
  model: CatalogModel | undefined,
  workspaceId: string | undefined,
): WorkflowNode {
  return {
    id,
    type: "agent",
    typeVersion: 1,
    inputs: [],
    outputSchema: closedObject(),
    agent: {
      provider: "codex",
      model: model?.id ?? "",
      effort: model?.recommendedEffort ?? model?.efforts[0] ?? "",
      workspaceId: workspaceId ?? "",
      access: "read_only",
      sessionMode: "new",
      instruction: "描述该 Agent 需要完成的任务。",
      timeout: "PT5M",
      retry: { maximumAttempts: 1 },
    },
  };
}

export function parseWorkflowFile(text: string): WorkflowDocument {
  let raw: unknown;
  try {
    raw = JSON.parse(text);
  } catch {
    throw new Error("文件不是有效的 JSON");
  }
  if (!raw || typeof raw !== "object") throw new Error("工作流根节点必须是对象");
  const document = raw as Partial<WorkflowDocument>;
  if (
    document.apiVersion !== "orchestration.misaka.dev/v1" ||
    document.kind !== "Workflow" ||
    !document.metadata ||
    !document.spec ||
    !Array.isArray(document.spec.nodes) ||
    !Array.isArray(document.spec.transitions)
  ) {
    throw new Error("文件不符合 V2 Workflow 基础结构");
  }
  const ids = new Set<string>();
  for (const node of document.spec.nodes) {
    if (!node || typeof node.id !== "string" || !node.id.trim()) {
      throw new Error("每个节点都必须包含非空 id");
    }
    if (ids.has(node.id)) throw new Error(`节点 id 重复：${node.id}`);
    ids.add(node.id);
  }
  return raw as WorkflowDocument;
}

export function nextVersion(document: WorkflowDocument, version: number): WorkflowDocument {
  return {
    ...document,
    metadata: {
      ...document.metadata,
      version,
    },
  };
}

export function transitionFor(
  from: string,
  to: string,
  existing: WorkflowTransition[],
): WorkflowTransition {
  const base = `${from}-${to}`;
  let id = base;
  let suffix = 2;
  while (existing.some((item) => item.id === id)) {
    id = `${base}-${suffix++}`;
  }
  return { id, from, to, on: "succeeded", priority: 100 };
}
