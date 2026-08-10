"use strict";

const terminalStatuses = new Set(["succeeded", "failed", "cancelled", "interrupted"]);
const identifierPattern = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/;

const state = {
  providers: [],
  workspaces: {},
  tasks: [],
  runId: null,
  pollTimer: null,
  pollInFlight: false,
  pollFailures: 0,
  lastRunStatus: null,
  devRevision: null,
  devReloadTimer: null,
};

const byId = (id) => document.getElementById(id);
const elements = {
  coreDot: byId("coreDot"),
  coreStatus: byId("coreStatus"),
  providerCount: byId("providerCount"),
  workspaceCount: byId("workspaceCount"),
  currentTaskCount: byId("currentTaskCount"),
  overviewRunStatus: byId("overviewRunStatus"),
  sidebarProviderCount: byId("sidebarProviderCount"),
  sidebarWorkspaceCount: byId("sidebarWorkspaceCount"),
  catalogTitle: byId("catalogTitle"),
  catalogSummary: byId("catalogSummary"),
  providerChips: byId("providerChips"),
  refreshCatalogBtn: byId("refreshCatalogBtn"),
  taskList: byId("taskList"),
  taskCount: byId("taskCount"),
  formMessage: byId("formMessage"),
  workflowName: byId("workflowName"),
  maxConcurrency: byId("maxConcurrency"),
  failurePolicy: byId("failurePolicy"),
  validateBtn: byId("validateBtn"),
  submitBtn: byId("submitBtn"),
  sampleBtn: byId("sampleBtn"),
  addTaskBtn: byId("addTaskBtn"),
  emptyRun: byId("emptyRun"),
  runDetails: byId("runDetails"),
  runStatus: byId("runStatus"),
  runId: byId("runId"),
  runProgress: byId("runProgress"),
  runSummary: byId("runSummary"),
  taskResults: byId("taskResults"),
  cancelBtn: byId("cancelBtn"),
  toast: byId("toast"),
};

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function setFormMessage(message, kind) {
  elements.formMessage.textContent = message;
  elements.formMessage.className = "form-message" + (kind ? " " + kind : "");
}

function showToast(message, isError) {
  elements.toast.textContent = message;
  elements.toast.className = "toast show" + (isError ? " error" : "");
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => {
    elements.toast.className = "toast";
  }, 3200);
}

function setButtonBusy(button, busy, busyLabel) {
  if (busy) {
    button.dataset.idleHtml = button.innerHTML;
    button.dataset.wasDisabled = String(button.disabled);
    button.textContent = busyLabel;
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    return;
  }
  if (button.dataset.idleHtml) {
    button.innerHTML = button.dataset.idleHtml;
    delete button.dataset.idleHtml;
  }
  button.disabled = button.dataset.wasDisabled === "true";
  delete button.dataset.wasDisabled;
  button.removeAttribute("aria-busy");
}

async function api(path, options) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  let payload;
  try {
    payload = await response.json();
  } catch (_error) {
    throw new Error("服务返回了无法解析的响应");
  }
  if (!response.ok) {
    const detail = Array.isArray(payload.detail)
      ? payload.detail.map((item) => item.msg || JSON.stringify(item)).join("；")
      : payload.detail;
    const error = new Error(detail || payload.code || "请求失败");
    error.code = payload.code || "request_failed";
    error.status = response.status;
    throw error;
  }
  return payload;
}

function providerNames() {
  return state.providers
    .filter((item) => item.available !== false)
    .map((item) => item.name);
}

function workspaceIds() {
  return Object.keys(state.workspaces);
}

function updateCatalogSummary() {
  const providerCount = providerNames().length;
  const workspaceCount = workspaceIds().length;
  const modelCount = state.providers.reduce(
    (total, provider) => total + (Array.isArray(provider.models) ? provider.models.length : 0),
    0
  );
  const unavailableCount = state.providers.length - providerCount;
  elements.providerCount.textContent = String(providerCount);
  elements.workspaceCount.textContent = String(workspaceCount);
  elements.sidebarProviderCount.textContent = String(providerCount);
  elements.sidebarWorkspaceCount.textContent = String(workspaceCount);
  elements.catalogTitle.textContent = unavailableCount
    ? "执行目录部分可用"
    : "执行目录已就绪";
  elements.catalogSummary.textContent = providerCount + " 个 Provider · " +
    modelCount + " 个任务模型 · " + workspaceCount + " 个授权工作区" +
    (unavailableCount ? " · " + unavailableCount + " 个 Provider 不可用" : "");
  elements.providerChips.innerHTML = state.providers.map((provider) => {
    const available = provider.available !== false;
    const models = Array.isArray(provider.models) ? provider.models.length : 0;
    const label = provider.name + (models ? " · " + models + " models" : "");
    const title = available
      ? label
      : (provider.error && provider.error.message) || "Provider 不可用";
    return '<span class="provider-chip ' + (available ? "available" : "unavailable") +
      '" title="' + escapeHtml(title) + '"><i></i>' + escapeHtml(label) + '</span>';
  }).join("");
}

function providerOptions(selected) {
  const names = providerNames();
  if (!names.length) return '<option value="">无可用 Provider</option>';
  return names.map((name) =>
    '<option value="' + escapeHtml(name) + '"' +
    (name === selected ? " selected" : "") + ">" +
    escapeHtml(name) + "</option>"
  ).join("");
}

function workspaceOptions(selected) {
  const ids = workspaceIds();
  if (!ids.length) return '<option value="">无可用工作区</option>';
  return ids.map((id) =>
    '<option value="' + escapeHtml(id) + '"' +
    (id === selected ? " selected" : "") + ">" +
    escapeHtml(id) + "</option>"
  ).join("");
}

function providerModels(providerName) {
  const provider = state.providers.find((item) => item.name === providerName);
  return provider && provider.available !== false && Array.isArray(provider.models)
    ? provider.models
    : [];
}

function modelTypeOptions(providerName, selected) {
  const types = [...new Set(
    providerModels(providerName).map((model) => model.model_type).filter(Boolean)
  )].sort((left, right) => left.localeCompare(right));
  const placeholder = types.length ? "请选择模型类型" : "核心未配置模型类型";
  return [
    '<option value=""', selected ? "" : " selected", ' disabled>', placeholder, '</option>',
    ...types.map((modelType) =>
      '<option value="' + escapeHtml(modelType) + '"' +
      (modelType === selected ? " selected" : "") + ">" +
      escapeHtml(modelType) + "</option>"
    )
  ].join("");
}

function modelOptions(providerName, modelType, selected) {
  const models = providerModels(providerName).filter(
    (model) => model.model_type === modelType
  );
  const placeholder = modelType
    ? (models.length ? "请选择模型" : "该类型没有可用模型")
    : "请先选择模型类型";
  return [
    '<option value=""', selected ? "" : " selected", ' disabled>', placeholder, '</option>',
    ...models.map((model) => {
      const text = model.label && model.label !== model.id
        ? model.label + " · " + model.id
        : model.id;
      return '<option value="' + escapeHtml(model.id) + '"' +
        (model.id === selected ? " selected" : "") + ">" +
        escapeHtml(text) + "</option>";
    })
  ].join("");
}

function effortOptions(providerName, modelId, selected) {
  const model = providerModels(providerName).find((item) => item.id === modelId);
  const efforts = model && Array.isArray(model.efforts) ? model.efforts : [];
  const placeholder = model ? "请选择 effort" : "请先选择模型";
  return [
    '<option value=""', selected ? "" : " selected", ' disabled>', placeholder, '</option>',
    ...efforts.map((effort) =>
      '<option value="' + escapeHtml(effort) + '"' +
      (effort === selected ? " selected" : "") + ">" +
      escapeHtml(effort) + "</option>"
    )
  ].join("");
}

function dependencyOptions(task, index) {
  const selected = new Set(task.depends_on);
  return state.tasks
    .map((candidate, candidateIndex) => ({ candidate, candidateIndex }))
    .filter((item) => item.candidateIndex !== index && item.candidate.id)
    .map((item) => {
      const id = item.candidate.id;
      return '<option value="' + escapeHtml(id) + '"' +
        (selected.has(id) ? " selected" : "") + ">" +
        escapeHtml(id) + "</option>";
    })
    .join("");
}

function writeSupported(providerName) {
  const provider = state.providers.find((item) => item.name === providerName);
  return Boolean(provider && provider.capabilities && provider.capabilities.workspace_write_mode);
}

function nextTaskId() {
  const knownIds = new Set(state.tasks.map((task) => task.id));
  let index = 1;
  while (knownIds.has("task_" + index)) {
    index += 1;
  }
  return "task_" + index;
}

function newTask(index) {
  const availableProviders = providerNames();
  const defaultProvider = availableProviders.includes("codex")
    ? "codex"
    : (availableProviders[0] || "");
  return {
    id: Number.isInteger(index) ? "task_" + (index + 1) : nextTaskId(),
    provider: defaultProvider,
    role: "worker",
    workspace_id: workspaceIds()[0] || "",
    access: "read_only",
    depends_on: [],
    prompt_template: "",
    model_type: "",
    model: "",
    effort: "",
    timeout_seconds: 300,
    output_schema_text: "",
  };
}

function renderTasks() {
  elements.taskList.replaceChildren();
  elements.taskCount.textContent = state.tasks.length + " TASKS";
  elements.currentTaskCount.textContent = String(state.tasks.length);

  state.tasks.forEach((task, index) => {
    if (index > 0) {
      const edge = document.createElement("div");
      edge.className = "task-edge";
      edge.textContent = task.depends_on.length
        ? "依赖 " + task.depends_on.join(", ")
        : "无依赖，可并行";
      elements.taskList.appendChild(edge);
    }

    const card = document.createElement("article");
    card.className = "task-card";
    const codexHidden = task.provider === "codex" ? "" : " hidden";
    const writeDisabled = writeSupported(task.provider) ? "" : " disabled";
    const modelReady = task.provider !== "codex" || Boolean(task.model && task.effort);
    const modelSummary = task.provider === "codex"
      ? (modelReady ? task.model + " · " + task.effort : "等待选择模型")
      : "Provider 默认模型";
    const dependencySummary = task.depends_on.length
      ? task.depends_on.length + " 个依赖"
      : "可立即调度";
    card.innerHTML = [
      '<div class="task-head">',
        '<div class="task-title">',
          '<span class="task-number">', String(index + 1).padStart(2, "0"), '</span>',
          '<div><strong>', escapeHtml(task.id || "未命名任务"), '</strong>',
          '<small>', escapeHtml(task.provider || "未选择 Provider"), ' · ', escapeHtml(task.role || "worker"), '</small>',
          '<div class="task-badges">',
            '<span>', escapeHtml(dependencySummary), '</span>',
            '<span>', task.access === "workspace_write" ? "工作区写入" : "只读执行", '</span>',
            '<span class="', modelReady ? "ready" : "attention", '">', escapeHtml(modelSummary), '</span>',
          '</div></div>',
        '</div>',
        '<div class="task-head-actions">',
          '<button class="task-action duplicate-task" type="button" title="复制任务" aria-label="复制任务">复制</button>',
          '<button class="task-action remove-task" type="button" title="删除任务" aria-label="删除任务">删除</button>',
        '</div>',
      '</div>',
      '<div class="task-grid">',
        '<div class="task-section-label span-4"><span>执行配置</span><small>身份、资源与调度边界</small></div>',
        '<label class="field"><span>任务 ID</span><input data-field="id" value="', escapeHtml(task.id), '" placeholder="analyze"></label>',
        '<label class="field"><span>Provider</span><select data-field="provider">', providerOptions(task.provider), '</select></label>',
        '<label class="field"><span>角色</span><input data-field="role" value="', escapeHtml(task.role), '" placeholder="analyst"></label>',
        '<label class="field"><span>工作区</span><select data-field="workspace_id">', workspaceOptions(task.workspace_id), '</select></label>',
        '<label class="field"><span>访问权限</span><select data-field="access">',
          '<option value="read_only"', task.access === "read_only" ? " selected" : "", '>只读</option>',
          '<option value="workspace_write"', task.access === "workspace_write" ? " selected" : "", writeDisabled, '>工作区写入</option>',
        '</select></label>',
        '<label class="field"><span>依赖任务（可多选）</span><select data-field="depends_on" multiple>', dependencyOptions(task, index), '</select></label>',
        '<label class="field"><span>超时（秒）</span><input data-field="timeout_seconds" type="number" min="1" max="86400" value="', escapeHtml(task.timeout_seconds), '"></label>',
        '<div class="codex-options', codexHidden, '">',
          '<div class="task-section-label span-4"><span>Codex 运行参数</span><small>每个任务显式传入，不使用默认模型</small></div>',
          '<label class="field"><span>模型类型（任务级必选）</span><select data-field="model_type">', modelTypeOptions(task.provider, task.model_type), '</select></label>',
          '<label class="field"><span>Codex 模型（任务级必选）</span><select data-field="model">', modelOptions(task.provider, task.model_type, task.model), '</select></label>',
          '<label class="field"><span>推理等级（任务级必选）</span><select data-field="effort">', effortOptions(task.provider, task.model, task.effort), '</select></label>',
        '</div>',
        '<div class="task-section-label span-4"><span>任务契约</span><small>提示词与结构化输出边界</small></div>',
        '<label class="field span-4 prompt-field"><span>任务提示词</span><textarea data-field="prompt_template" placeholder="描述这个任务要完成什么…">', escapeHtml(task.prompt_template), '</textarea>',
          '<small class="field-hint"><b data-role="prompt-count">', String(task.prompt_template.length), '</b> 字符 · 依赖输出使用 {{tasks.task_id.output}}</small></label>',
        '<div class="field span-4 schema-field">',
          '<div class="field-label-row"><span>输出 JSON Schema（可选）</span><div>',
            '<button class="inline-action format-schema" type="button">格式化</button>',
            '<button class="inline-action strict-schema" type="button">插入严格模板</button>',
          '</div></div>',
          '<textarea data-field="output_schema_text" spellcheck="false" placeholder=\'{"type":"object","required":["result"],"properties":{"result":{"type":"string"}},"additionalProperties":false}\'>', escapeHtml(task.output_schema_text), '</textarea>',
          '<small class="field-hint">Codex 对每一层 object 都要求 additionalProperties: false。</small>',
        '</div>',
      '</div>'
    ].join("");

    card.querySelector(".duplicate-task").addEventListener("click", () => {
      const duplicate = {
        ...task,
        id: nextTaskId(),
        depends_on: [...task.depends_on],
      };
      state.tasks.splice(index + 1, 0, duplicate);
      renderTasks();
      showToast("已复制任务：" + duplicate.id);
    });

    card.querySelector(".remove-task").addEventListener("click", () => {
      state.tasks.splice(index, 1);
      const knownIds = new Set(state.tasks.map((item) => item.id));
      state.tasks.forEach((item) => {
        item.depends_on = item.depends_on.filter((id) => knownIds.has(id));
      });
      renderTasks();
    });

    card.querySelector(".format-schema").addEventListener("click", () => {
      const textarea = card.querySelector('[data-field="output_schema_text"]');
      if (!textarea.value.trim()) {
        showToast("当前没有可格式化的 Schema", true);
        return;
      }
      try {
        const formatted = JSON.stringify(JSON.parse(textarea.value), null, 2);
        textarea.value = formatted;
        task.output_schema_text = formatted;
        showToast("Schema 已格式化");
      } catch (_error) {
        showToast("Schema 不是有效 JSON", true);
      }
    });

    card.querySelector(".strict-schema").addEventListener("click", () => {
      const template = JSON.stringify({
        type: "object",
        required: ["result"],
        properties: { result: { type: "string" } },
        additionalProperties: false,
      }, null, 2);
      task.output_schema_text = template;
      card.querySelector('[data-field="output_schema_text"]').value = template;
      showToast("已插入 Codex 严格 Schema 模板");
    });

    card.querySelectorAll("[data-field]").forEach((input) => {
      const field = input.dataset.field;
      if (field === "depends_on") {
        input.addEventListener("change", () => {
          task.depends_on = Array.from(input.selectedOptions).map((option) => option.value);
          renderTasks();
        });
        return;
      }
      input.addEventListener("input", () => {
        task[field] = field === "timeout_seconds" ? Number(input.value) : input.value;
        if (field === "id") {
          card.querySelector(".task-title strong").textContent = input.value || "未命名任务";
        }
        if (field === "prompt_template") {
          card.querySelector('[data-role="prompt-count"]').textContent = String(input.value.length);
        }
      });
      if (field === "id") {
        input.addEventListener("blur", renderTasks);
      }
      if (field === "provider") {
        input.addEventListener("change", () => {
          task.provider = input.value;
          if (!providerModels(task.provider).some((model) => model.id === task.model)) {
            task.model_type = "";
            task.model = "";
            task.effort = "";
          }
          if (task.access === "workspace_write" && !writeSupported(task.provider)) {
            task.access = "read_only";
          }
          renderTasks();
        });
      }
      if (field === "model_type") {
        input.addEventListener("change", () => {
          task.model_type = input.value;
          task.model = "";
          task.effort = "";
          renderTasks();
        });
      }
      if (field === "model") {
        input.addEventListener("change", () => {
          task.model = input.value;
          const model = providerModels(task.provider).find((item) => item.id === task.model);
          const efforts = model && Array.isArray(model.efforts) ? model.efforts : [];
          if (!efforts.includes(task.effort)) {
            task.effort = "";
          }
          renderTasks();
        });
      }
    });
    elements.taskList.appendChild(card);
  });
}

function loadAdditionSample() {
  const provider = providerNames().includes("codex") ? "codex" : (providerNames()[0] || "");
  const workspace = workspaceIds()[0] || "";
  elements.workflowName.value = "两阶段加法流水线";
  elements.maxConcurrency.value = "1";
  elements.failurePolicy.value = "fail_fast";
  state.tasks = [
    {
      ...newTask(0),
      id: "extract_formulas",
      provider,
      workspace_id: workspace,
      role: "formula_reader",
      prompt_template: "读取工作区 multi-agent/examples/addition_pipeline/inputs/ 下所有 .txt 文件。每个文件是一条整数加法公式。不要计算，只按文件名排序并输出公式 JSON。",
      output_schema_text: JSON.stringify({
        type: "object",
        required: ["formulas"],
        properties: {
          formulas: {
            type: "array",
            items: {
              type: "object",
              required: ["source", "expression"],
              properties: {
                source: { type: "string" },
                expression: { type: "string" },
              },
              additionalProperties: false,
            },
          },
        },
        additionalProperties: false,
      }, null, 2),
    },
    {
      ...newTask(1),
      id: "calculate_results",
      provider,
      workspace_id: workspace,
      role: "calculator",
      depends_on: ["extract_formulas"],
      prompt_template: "读取下面的上游任务输出，逐条计算加法并输出结果 JSON。\\n{{tasks.extract_formulas.output}}",
      output_schema_text: JSON.stringify({
        type: "object",
        required: ["results"],
        properties: {
          results: {
            type: "array",
            items: {
              type: "object",
              required: ["source", "expression", "result"],
              properties: {
                source: { type: "string" },
                expression: { type: "string" },
                result: { type: "integer" },
              },
              additionalProperties: false,
            },
          },
        },
        additionalProperties: false,
      }, null, 2),
    },
  ];
  renderTasks();
  setFormMessage(
    provider === "codex"
      ? "示例已加载。请为每个 Codex 任务从目录中选择模型和 effort。"
      : "示例已加载，可继续编辑。",
    ""
  );
}

function validateCodexOutputSchema(schema, taskId) {
  function fail(path, message) {
    throw new Error("Codex 任务 " + taskId + " 的输出 Schema：" + path + " " + message);
  }

  function visit(node, path, requireObject) {
    if (!node || typeof node !== "object" || Array.isArray(node)) {
      fail(path, "必须是 JSON Schema 对象");
    }

    const nodeTypes = Array.isArray(node.type) ? node.type : [node.type];
    const isObject = nodeTypes.includes("object") || Object.hasOwn(node, "properties");
    if (requireObject && !isObject) fail(path, "根节点 type 必须为 object");

    if (isObject) {
      if (!node.properties || typeof node.properties !== "object" || Array.isArray(node.properties)) {
        fail(path + ".properties", "必须是对象");
      }
      if (node.additionalProperties !== false) {
        fail(path + ".additionalProperties", "必须显式设置为 false");
      }
      if (!Array.isArray(node.required)) {
        fail(path + ".required", "必须列出 properties 中的所有字段");
      }
      const missing = Object.keys(node.properties).filter((name) => !node.required.includes(name));
      if (missing.length) {
        fail(path + ".required", "缺少字段：" + missing.join(", "));
      }
      Object.entries(node.properties).forEach(([name, child]) => {
        visit(child, path + ".properties." + name, false);
      });
    }

    if (nodeTypes.includes("array")) {
      if (!node.items || typeof node.items !== "object" || Array.isArray(node.items)) {
        fail(path + ".items", "数组必须声明 items");
      }
      visit(node.items, path + ".items", false);
    }

    if (node.$defs !== undefined) {
      if (!node.$defs || typeof node.$defs !== "object" || Array.isArray(node.$defs)) {
        fail(path + ".$defs", "必须是对象");
      }
      Object.entries(node.$defs).forEach(([name, child]) => {
        visit(child, path + ".$defs." + name, false);
      });
    }

    ["anyOf", "oneOf", "allOf"].forEach((keyword) => {
      if (node[keyword] === undefined) return;
      if (!Array.isArray(node[keyword])) fail(path + "." + keyword, "必须是数组");
      node[keyword].forEach((child, index) => {
        visit(child, path + "." + keyword + "[" + index + "]", false);
      });
    });
  }

  visit(schema, "$", true);
}

function buildWorkflow() {
  const name = elements.workflowName.value.trim();
  if (!name) throw new Error("工作流名称不能为空");
  if (!state.tasks.length) throw new Error("至少需要一个任务");

  const ids = state.tasks.map((task) => task.id.trim());
  if (new Set(ids).size !== ids.length) throw new Error("任务 ID 不能重复");
  ids.forEach((id) => {
    if (!identifierPattern.test(id)) {
      throw new Error("任务 ID 只能包含字母、数字、点、下划线和连字符：" + id);
    }
  });
  const knownIds = new Set(ids);

  const tasks = state.tasks.map((task) => {
    if (!task.provider) throw new Error("任务 " + task.id + " 未选择 Provider");
    if (!task.workspace_id) throw new Error("任务 " + task.id + " 未选择工作区");
    if (!task.prompt_template.trim()) throw new Error("任务 " + task.id + " 缺少提示词");
    task.depends_on.forEach((dependency) => {
      if (!knownIds.has(dependency)) throw new Error("任务 " + task.id + " 引用了未知依赖 " + dependency);
      if (dependency === task.id) throw new Error("任务不能依赖自身：" + task.id);
    });

    const providerOptions = {};
    if (task.provider === "codex") {
      if (!task.model_type) throw new Error("Codex 任务 " + task.id + " 必须选择模型类型");
      const selectedModel = providerModels(task.provider).find(
        (model) => model.id === task.model && model.model_type === task.model_type
      );
      if (!selectedModel) throw new Error("Codex 任务 " + task.id + " 必须从列表选择模型");
      if (!task.effort) throw new Error("Codex 任务 " + task.id + " 必须显式选择 effort");
      if (Array.isArray(selectedModel.efforts) && !selectedModel.efforts.includes(task.effort)) {
        throw new Error("Codex 任务 " + task.id + " 的 effort 不适用于所选模型");
      }
      providerOptions.model = selectedModel.id;
      providerOptions.effort = task.effort;
    }

    let outputSchema = null;
    if (task.output_schema_text.trim()) {
      try {
        outputSchema = JSON.parse(task.output_schema_text);
      } catch (_error) {
        throw new Error("任务 " + task.id + " 的输出 Schema 不是有效 JSON");
      }
      if (task.provider === "codex") {
        validateCodexOutputSchema(outputSchema, task.id);
      }
    }

    return {
      id: task.id.trim(),
      depends_on: [...task.depends_on],
      provider: task.provider,
      role: task.role.trim() || "worker",
      prompt_template: task.prompt_template.trim(),
      workspace_id: task.workspace_id,
      access: task.access,
      output_schema: outputSchema,
      timeout_seconds: Number(task.timeout_seconds) || 300,
      retry_policy: { max_attempts: 1, idempotent: false },
      provider_options: providerOptions,
    };
  });

  return {
    name,
    tasks,
    max_concurrency: Number(elements.maxConcurrency.value) || 1,
    failure_policy: elements.failurePolicy.value,
  };
}

async function validateWorkflow(showSuccess) {
  if (showSuccess) {
    setButtonBusy(elements.validateBtn, true, "正在校验…");
  }
  try {
    const workflow = buildWorkflow();
    const result = await api("/api/workflows/validate", {
      method: "POST",
      body: JSON.stringify(workflow),
    });
    if (showSuccess) {
      setFormMessage("校验通过：" + result.task_count + " 个任务。", "success");
      showToast("工作流校验通过");
    }
    return workflow;
  } finally {
    if (showSuccess) {
      setButtonBusy(elements.validateBtn, false);
      elements.validateBtn.disabled = elements.coreStatus.textContent !== "已连接";
    }
  }
}

async function submitWorkflow() {
  setButtonBusy(elements.submitBtn, true, "正在提交…");
  try {
    const workflow = await validateWorkflow(false);
    const run = await api("/api/runs", {
      method: "POST",
      body: JSON.stringify(workflow),
    });
    state.runId = run.id;
    state.lastRunStatus = null;
    elements.emptyRun.classList.add("hidden");
    elements.runDetails.classList.remove("hidden");
    elements.runId.textContent = run.id;
    setFormMessage("运行已提交：" + run.id, "success");
    showToast("工作流已提交");
    await refreshRun();
    if (!terminalStatuses.has(state.lastRunStatus)) {
      startPolling();
    }
  } catch (error) {
    setFormMessage(error.message, "error");
    showToast(error.message, true);
  } finally {
    setButtonBusy(elements.submitBtn, false);
    elements.submitBtn.disabled = elements.coreStatus.textContent !== "已连接";
  }
}

function prettyOutput(value) {
  if (!value) return "等待输出…";
  try {
    return JSON.stringify(JSON.parse(value), null, 2);
  } catch (_error) {
    return value;
  }
}

function renderRun(run, tasks) {
  const status = run.status || "unknown";
  const statusLabels = {
    queued: "排队中",
    running: "运行中",
    succeeded: "已成功",
    failed: "失败",
    cancelled: "已取消",
    interrupted: "已中断",
  };
  const taskStatusLabels = {
    pending: "等待中",
    ready: "可调度",
    running: "运行中",
    awaiting_approval: "等待审批",
    succeeded: "已成功",
    failed: "失败",
    blocked: "已阻塞",
    cancelled: "已取消",
    interrupted: "已中断",
  };
  elements.runStatus.textContent = statusLabels[status] || status.toUpperCase();
  elements.runStatus.className = "run-badge " + status;
  elements.overviewRunStatus.textContent = statusLabels[status] || status;
  const terminal = tasks.filter((task) =>
    ["succeeded", "failed", "blocked", "cancelled", "interrupted"].includes(task.status)
  ).length;
  const percent = tasks.length ? Math.round((terminal / tasks.length) * 100) : 0;
  const running = tasks.filter((task) => task.status === "running").length;
  const failed = tasks.filter((task) => ["failed", "blocked"].includes(task.status)).length;
  elements.runProgress.firstElementChild.style.width = percent + "%";
  elements.runSummary.innerHTML = [
    '<strong>', String(percent), '%</strong>',
    '<span>', String(terminal), ' / ', String(tasks.length), ' 个任务已结束</span>',
    running ? '<span>· ' + running + ' 个运行中</span>' : '',
    failed ? '<span class="summary-error">· ' + failed + ' 个异常</span>' : '',
    run.error ? '<span class="summary-error">· ' + escapeHtml(run.error) + '</span>' : '',
  ].join("");
  elements.cancelBtn.disabled = terminalStatuses.has(status);

  elements.taskResults.innerHTML = tasks.map((task) => {
    const output = task.final_output || task.error_message || "";
    const spec = task.spec || {};
    return [
      '<article class="result-card">',
        '<div class="result-head">',
          '<div><strong>', escapeHtml(task.task_id), '</strong>',
          '<small>', escapeHtml(spec.provider || "unknown"), ' · ', escapeHtml(spec.role || "worker"),
          ' · ', String(task.attempt_count || 0), ' 次尝试</small></div>',
          '<span class="result-status ', escapeHtml(task.status), '">', escapeHtml(taskStatusLabels[task.status] || task.status), '</span>',
        '</div>',
        task.error_code ? '<div class="result-error-code">' + escapeHtml(task.error_code) + '</div>' : '',
        '<pre class="result-output', output ? "" : " result-empty", '">', escapeHtml(prettyOutput(output)), '</pre>',
        output ? '<button class="copy-output" type="button" data-output="' + escapeHtml(output) + '">复制输出</button>' : '',
      '</article>'
    ].join("");
  }).join("");

  elements.taskResults.querySelectorAll(".copy-output").forEach((button) => {
    button.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(button.dataset.output || "");
        showToast("任务输出已复制");
      } catch (_error) {
        showToast("无法访问剪贴板，请手动复制", true);
      }
    });
  });

  if (terminalStatuses.has(status)) {
    stopPolling();
    if (state.lastRunStatus !== status) {
      showToast("运行已结束：" + (statusLabels[status] || status), status !== "succeeded");
    }
  }
  state.lastRunStatus = status;
}

async function refreshRun() {
  if (!state.runId || state.pollInFlight) return;
  state.pollInFlight = true;
  try {
    const [run, tasks] = await Promise.all([
      api("/api/runs/" + encodeURIComponent(state.runId)),
      api("/api/runs/" + encodeURIComponent(state.runId) + "/tasks"),
    ]);
    renderRun(run, tasks);
    state.pollFailures = 0;
  } catch (error) {
    state.pollFailures += 1;
    if (state.pollFailures >= 3) {
      stopPolling();
      showToast("运行状态连续刷新失败，请检查核心服务", true);
    }
  } finally {
    state.pollInFlight = false;
  }
}

function startPolling() {
  stopPolling();
  state.pollFailures = 0;
  state.pollTimer = window.setInterval(refreshRun, 1000);
}

function stopPolling() {
  if (state.pollTimer) {
    window.clearInterval(state.pollTimer);
    state.pollTimer = null;
  }
}

function reconcileTasksWithCatalog() {
  const availableProviders = providerNames();
  const fallbackProvider = availableProviders.includes("codex")
    ? "codex"
    : (availableProviders[0] || "");
  const availableWorkspaces = workspaceIds();
  state.tasks.forEach((task) => {
    if (!availableProviders.includes(task.provider)) {
      task.provider = fallbackProvider;
      task.model_type = "";
      task.model = "";
      task.effort = "";
    }
    if (!availableWorkspaces.includes(task.workspace_id)) {
      task.workspace_id = availableWorkspaces[0] || "";
    }
    const selectedModel = providerModels(task.provider).find(
      (model) => model.id === task.model && model.model_type === task.model_type
    );
    if (!selectedModel) {
      task.model_type = "";
      task.model = "";
      task.effort = "";
    } else if (!selectedModel.efforts.includes(task.effort)) {
      task.effort = "";
    }
  });
}

async function loadExecutionCatalog(showFeedback) {
  setButtonBusy(elements.refreshCatalogBtn, true, "…");
  try {
    const [, providers, workspaces] = await Promise.all([
      api("/api/core/health"),
      api("/api/providers"),
      api("/api/workspaces"),
    ]);
    state.providers = providers;
    state.workspaces = workspaces;
    reconcileTasksWithCatalog();
    updateCatalogSummary();
    elements.coreDot.className = "status-dot online";
    elements.coreStatus.textContent = "已连接";
    elements.validateBtn.disabled = false;
    elements.submitBtn.disabled = false;
    if (state.tasks.length) renderTasks();
    if (showFeedback) showToast("执行目录已刷新");
    return true;
  } catch (error) {
    elements.coreDot.className = "status-dot offline";
    elements.coreStatus.textContent = "不可用";
    elements.catalogTitle.textContent = "执行目录不可用";
    elements.catalogSummary.textContent = error.message;
    elements.providerChips.replaceChildren();
    elements.validateBtn.disabled = true;
    elements.submitBtn.disabled = true;
    if (showFeedback) showToast("刷新失败：" + error.message, true);
    return false;
  } finally {
    setButtonBusy(elements.refreshCatalogBtn, false);
  }
}

async function initialize() {
  const connected = await loadExecutionCatalog(false);
  if (connected) {
    loadAdditionSample();
  } else {
    setFormMessage("无法连接核心服务，请检查启动状态后刷新执行目录。", "error");
    state.tasks = [newTask(0)];
    renderTasks();
  }
}

async function startDevLiveReload() {
  try {
    const response = await fetch("/api/dev/revision", { cache: "no-store" });
    if (!response.ok) return;
    const payload = await response.json();
    if (!payload.enabled) return;
    state.devRevision = payload.revision;
  } catch (_error) {
    return;
  }

  state.devReloadTimer = window.setInterval(async () => {
    try {
      const response = await fetch("/api/dev/revision", { cache: "no-store" });
      if (!response.ok) return;
      const payload = await response.json();
      if (!payload.enabled) return;
      if (state.devRevision && payload.revision !== state.devRevision) {
        window.location.reload();
        return;
      }
      state.devRevision = payload.revision;
    } catch (_error) {
      // The reload supervisor may briefly restart the server. Retry next tick.
    }
  }, 900);
}

elements.addTaskBtn.addEventListener("click", () => {
  state.tasks.push(newTask());
  renderTasks();
});
elements.refreshCatalogBtn.addEventListener("click", () => loadExecutionCatalog(true));
elements.sampleBtn.addEventListener("click", loadAdditionSample);
elements.validateBtn.addEventListener("click", async () => {
  try {
    await validateWorkflow(true);
  } catch (error) {
    setFormMessage(error.message, "error");
    showToast(error.message, true);
  }
});
elements.submitBtn.addEventListener("click", submitWorkflow);
elements.cancelBtn.addEventListener("click", async () => {
  if (!state.runId) return;
  setButtonBusy(elements.cancelBtn, true, "正在取消…");
  try {
    await api("/api/runs/" + encodeURIComponent(state.runId) + "/cancel", { method: "POST" });
    await refreshRun();
  } catch (error) {
    showToast("取消失败：" + error.message, true);
  } finally {
    setButtonBusy(elements.cancelBtn, false);
    elements.cancelBtn.disabled = terminalStatuses.has(state.lastRunStatus);
  }
});
window.addEventListener("beforeunload", () => {
  stopPolling();
  if (state.devReloadTimer) {
    window.clearInterval(state.devReloadTimer);
  }
});

startDevLiveReload();
initialize();
