import { describe, expect, it } from "vitest";
import {
  createAgentNode,
  createWorkflow,
  describeWorkflowInput,
  formatWorkflowInputExample,
  nextVersion,
  parseWorkflowFile,
  parseWorkflowInput,
  transitionFor,
} from "./workflow";

describe("workflow editor contract", () => {
  it("never invents a default model or effort", () => {
    const node = createAgentNode("agent-1", undefined, undefined);

    expect(node.agent?.model).toBe("");
    expect(node.agent?.effort).toBe("");
    expect(node.agent?.workspaceId).toBe("");
  });

  it("imports only the V2 workflow envelope", () => {
    const document = createWorkflow();
    document.spec.nodes.push(createAgentNode("agent-1", undefined, undefined));

    expect(parseWorkflowFile(JSON.stringify(document))).toEqual(document);
    expect(() => parseWorkflowFile('{"kind":"Workflow"}')).toThrow(
      "不符合 V2 Workflow",
    );
  });

  it("updates immutable template metadata and generates unique edge ids", () => {
    const versioned = nextVersion(createWorkflow(), 3);
    const first = transitionFor("a", "b", []);
    const second = transitionFor("a", "b", [first]);

    expect(versioned.metadata.version).toBe(3);
    expect(first.id).toBe("a-b");
    expect(second.id).toBe("a-b-2");
  });

  it("accepts only JSON objects as workflow input", () => {
    expect(parseWorkflowInput('{"left": 3, "right": 4}')).toEqual({
      left: 3,
      right: 4,
    });
    expect(() => parseWorkflowInput("[]")).toThrow(
      "工作流输入必须是有效 JSON 对象",
    );
    expect(() => parseWorkflowInput("not-json")).toThrow(
      "工作流输入必须是有效 JSON 对象",
    );
  });

  it("describes input fields and generates an editable example", () => {
    const schema = {
      type: "object",
      properties: {
        requestId: { type: "string", description: "请求标识" },
        release: { type: "boolean", default: true },
      },
      required: ["requestId"],
      additionalProperties: false,
    };

    expect(describeWorkflowInput(schema)).toEqual([
      { name: "requestId", type: "string", required: true, description: "请求标识" },
      {
        name: "release",
        type: "boolean",
        required: false,
        description: "请填写 boolean 类型的值；示例已自动生成",
      },
    ]);
    expect(JSON.parse(formatWorkflowInputExample(schema))).toEqual({
      requestId: "requestId-example",
      release: true,
    });
  });
});
