import type {
  CatalogModel,
  JsonObject,
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

export function parseWorkflowInput(text: string): JsonObject {
  let raw: unknown;
  try {
    raw = JSON.parse(text);
  } catch {
    throw new Error("工作流输入必须是有效 JSON 对象");
  }
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
    throw new Error("工作流输入必须是有效 JSON 对象");
  }
  return raw as JsonObject;
}

export interface WorkflowInputField {
  name: string;
  type: string;
  required: boolean;
  description: string;
}

export function describeWorkflowInput(schema: JsonObject): WorkflowInputField[] {
  const properties = asObject(schema.properties);
  const required = new Set(
    Array.isArray(schema.required)
      ? schema.required.filter((item): item is string => typeof item === "string")
      : [],
  );
  return Object.entries(properties).map(([name, rawProperty]) => {
    const property = asObject(rawProperty);
    const type = schemaType(property);
    return {
      name,
      type,
      required: required.has(name),
      description:
        stringValue(property.description) ||
        stringValue(property.title) ||
        `请填写 ${type} 类型的值；示例已自动生成`,
    };
  });
}

export function formatWorkflowInputExample(schema: JsonObject): string {
  return JSON.stringify(exampleForSchema(schema, "input"), null, 2);
}

function exampleForSchema(schema: JsonObject, fieldName: string): unknown {
  if ("default" in schema) return schema.default;
  if ("const" in schema) return schema.const;
  if (Array.isArray(schema.examples) && schema.examples.length) return schema.examples[0];
  if (Array.isArray(schema.enum) && schema.enum.length) return schema.enum[0];

  const alternatives = Array.isArray(schema.oneOf)
    ? schema.oneOf
    : Array.isArray(schema.anyOf)
      ? schema.anyOf
      : [];
  const firstAlternative = asObject(alternatives[0]);
  if (Object.keys(firstAlternative).length) {
    return exampleForSchema(firstAlternative, fieldName);
  }

  switch (schemaType(schema)) {
    case "object":
      return Object.fromEntries(
        Object.entries(asObject(schema.properties)).map(([name, property]) => [
          name,
          exampleForSchema(asObject(property), name),
        ]),
      );
    case "array": {
      const itemSchema = asObject(schema.items);
      return Number(schema.minItems ?? 0) > 0 && Object.keys(itemSchema).length
        ? [exampleForSchema(itemSchema, fieldName)]
        : [];
    }
    case "integer":
    case "number":
      return typeof schema.minimum === "number" ? schema.minimum : 0;
    case "boolean":
      return false;
    case "null":
      return null;
    default:
      return `${fieldName}-example`;
  }
}

function schemaType(schema: JsonObject): string {
  if (typeof schema.type === "string") return schema.type;
  if (Array.isArray(schema.type)) {
    const types = schema.type.filter((item): item is string => typeof item === "string");
    return types.join(" | ") || "unknown";
  }
  if (schema.properties) return "object";
  return "unknown";
}

function asObject(value: unknown): JsonObject {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as JsonObject)
    : {};
}

function stringValue(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
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
