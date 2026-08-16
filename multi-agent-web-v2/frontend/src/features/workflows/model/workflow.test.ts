import { describe, expect, it } from "vitest";
import {
  createAgentNode,
  createWorkflow,
  nextVersion,
  parseWorkflowFile,
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
});
