import type { JsonObject, WorkflowNode } from "../../../shared/types";

export interface NodeInspectorValue {
  instruction?: string;
  model?: string;
  effort?: string;
  workspaceId?: string;
  access?: "read_only" | "workspace_write";
  sessionMode?: "new" | "resume";
  timeout?: string;
  maximumAttempts?: number | null;
  predecessors?: string[];
  outputSchema?: string;
}

export class InvalidNodeOutputSchemaError extends Error {}

export function updateNodeFromInspector(
  node: WorkflowNode,
  value: NodeInspectorValue,
): WorkflowNode {
  const outputSchema = parseOutputSchema(value.outputSchema, node.outputSchema);
  return {
    ...node,
    outputSchema,
    agent: node.agent
      ? {
          ...node.agent,
          instruction: value.instruction ?? node.agent.instruction,
          model: value.model ?? node.agent.model,
          effort: value.effort ?? node.agent.effort,
          workspaceId: value.workspaceId ?? node.agent.workspaceId,
          access: value.access ?? node.agent.access,
          sessionMode: value.sessionMode ?? node.agent.sessionMode,
          timeout: value.timeout ?? node.agent.timeout,
          retry: {
            maximumAttempts:
              value.maximumAttempts ?? node.agent.retry.maximumAttempts,
          },
        }
      : node.agent,
  };
}

function parseOutputSchema(value: string | undefined, fallback: JsonObject): JsonObject {
  if (value === undefined) return fallback;
  let parsed: unknown;
  try {
    parsed = JSON.parse(value);
  } catch {
    throw new InvalidNodeOutputSchemaError("输出契约必须是有效 JSON 对象");
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new InvalidNodeOutputSchemaError("输出契约必须是有效 JSON 对象");
  }
  return parsed as JsonObject;
}
