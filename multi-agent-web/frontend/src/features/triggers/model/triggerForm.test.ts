import { describe, expect, it } from "vitest";
import {
  newTriggerValues,
  parseJsonObject,
  triggerValuesToDefinition,
} from "./triggerForm";

describe("trigger form contract", () => {
  it("builds the canonical Git source key and structured source config", () => {
    const values = newTriggerValues("git_commit");
    values.id = "watch-main";
    values.name = "Watch main";
    values.template_id = "flow";
    values.workspace_id = "aitools";
    values.remote = "origin";
    values.branch = "main";
    values.event_filter_text = '{"update_kind":"forward"}';
    values.input_mapping_text = '{"sha":"after_sha"}';

    expect(triggerValuesToDefinition(values)).toMatchObject({
      source_key: "aitools:origin:main",
      source_config: {
        workspace_id: "aitools",
        remote: "origin",
        branch: "main",
        fetch: true,
      },
      event_filter: { update_kind: "forward" },
      input_mapping: { sha: "after_sha" },
    });
  });

  it("rejects arrays and malformed JSON where an object is required", () => {
    expect(() => parseJsonObject("[]", "Payload")).toThrow("JSON 对象");
    expect(() => parseJsonObject("{", "Payload")).toThrow("有效的 JSON");
  });

  it("builds a webhook source config with endpoint source key", () => {
    const values = newTriggerValues("webhook");
    values.id = "github-hook";
    values.name = "GitHub hook";
    values.template_id = "flow";
    values.endpoint_key = "github-repo-a";
    values.secret_ref = "GITHUB_REPO_A";
    values.signature_algorithm = "sha256";
    values.allowed_ip_cidrs = "127.0.0.1/32, 10.0.0.0/8";
    values.max_payload_bytes = 4096;
    values.dedup_header = "x-github-delivery";
    values.dedup_window_seconds = 300;

    expect(triggerValuesToDefinition(values)).toMatchObject({
      source_key: "github-repo-a",
      source_config: {
        endpoint_key: "github-repo-a",
        secret_ref: "GITHUB_REPO_A",
        signature_header: "x-hub-signature-256",
        signature_algorithm: "sha256",
        require_signature: true,
        allowed_ip_cidrs: ["127.0.0.1/32", "10.0.0.0/8"],
        max_payload_bytes: 4096,
        dedup_header: "x-github-delivery",
        dedup_window_seconds: 300,
      },
    });
  });
});
