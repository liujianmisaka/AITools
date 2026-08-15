from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, HTTPException

app = FastAPI(title="Browser QA fake orchestration core")
_instances: dict[str, dict[str, Any]] = {}
_templates: dict[str, dict[str, Any]] = {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _template_record(workflow: dict[str, Any], *, created_at: str | None = None) -> dict[str, Any]:
    now = _now()
    return {
        "id": workflow["id"],
        "version": workflow["version"],
        "name": workflow["name"],
        "task_count": len(workflow.get("tasks", [])),
        "definition": deepcopy(workflow),
        "created_at": created_at or now,
        "updated_at": now,
        "archived_at": None,
    }


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/v1/providers")
async def providers() -> list[dict[str, Any]]:
    return [
        {
            "name": "codex",
            "started": False,
            "available": True,
            "models": [
                {
                    "id": "sensenova/deepseek-v4-flash",
                    "label": "DeepSeek V4 Flash (SenseNova)",
                    "model_type": "sensenova",
                    "efforts": ["low", "medium", "high", "xhigh", "max", "ultra"],
                    "default_effort": "medium",
                }
            ],
            "capabilities": {
                "read_only_mode": True,
                "workspace_write_mode": False,
                "structured_output": True,
            },
            "metadata": {
                "model_provider": "openai",
                "model_catalog": "codex_config",
                "model_count": 1,
            },
            "error": None,
        }
    ]


@app.get("/api/v1/workspaces")
async def workspaces() -> dict[str, str]:
    return {"aitools": "."}


@app.post("/api/v1/templates/validate")
async def validate(workflow: dict[str, Any]) -> dict[str, Any]:
    return {
        "valid": True,
        "template_id": workflow.get("id", "browser-qa"),
        "task_count": len(workflow.get("tasks", [])),
    }


@app.post("/api/v1/templates", status_code=201)
async def create_template(workflow: dict[str, Any]) -> dict[str, Any]:
    definition = deepcopy(workflow)
    definition.setdefault("id", uuid4().hex)
    definition.setdefault("version", 1)
    record = _template_record(definition)
    _templates[record["id"]] = record
    return deepcopy(record)


@app.get("/api/v1/templates")
async def list_templates(limit: int = 50, cursor: str | None = None) -> dict[str, Any]:
    del cursor
    items = [
        {key: value for key, value in record.items() if key != "definition"}
        for record in sorted(
            _templates.values(),
            key=lambda item: (item["updated_at"], item["id"]),
            reverse=True,
        )
        if record["archived_at"] is None
    ][:limit]
    return {"items": items, "next_cursor": None}


@app.get("/api/v1/templates/{template_id}")
async def get_template(template_id: str) -> dict[str, Any]:
    record = _templates.get(template_id)
    if record is None or record["archived_at"] is not None:
        raise HTTPException(status_code=404, detail="workflow template not found")
    return deepcopy(record)


@app.put("/api/v1/templates/{template_id}")
async def update_template(template_id: str, workflow: dict[str, Any]) -> dict[str, Any]:
    current = _templates.get(template_id)
    if current is None or current["archived_at"] is not None:
        raise HTTPException(status_code=404, detail="workflow template not found")
    if workflow.get("version") != current["version"]:
        raise HTTPException(
            status_code=409,
            detail="workflow template version conflict",
            headers={"X-Error-Code": "workflow_template_version_conflict"},
        )
    definition = deepcopy(workflow)
    definition["version"] = current["version"] + 1
    record = _template_record(definition, created_at=current["created_at"])
    _templates[template_id] = record
    return deepcopy(record)


@app.delete("/api/v1/templates/{template_id}")
async def archive_template(template_id: str) -> dict[str, Any]:
    record = _templates.get(template_id)
    if record is None:
        raise HTTPException(status_code=404, detail="workflow template not found")
    record["archived_at"] = _now()
    record["updated_at"] = record["archived_at"]
    return deepcopy(record)


async def _create_instance(
    workflow: dict[str, Any],
    *,
    template_id: str | None = None,
) -> dict[str, Any]:
    instance_id = uuid4().hex
    tasks = []
    for task in workflow.get("tasks", []):
        output = (
            '{"formulas":[{"source":"addition_01.txt","expression":"12 + 30"}]}'
            if task["id"] == "extract_formulas"
            else '{"results":[{"source":"addition_01.txt","expression":"12 + 30","result":42}]}'
        )
        tasks.append(
            {
                "id": f"{instance_id}:{task['id']}",
                "workflow_instance_id": instance_id,
                "task_id": task["id"],
                "status": "succeeded",
                "final_output": output,
                "error_message": None,
                "spec": deepcopy(task),
            }
        )
    now = _now()
    instance = {
        "id": instance_id,
        "template_id": template_id,
        "template_version": workflow.get("version") if template_id else None,
        "source": "template" if template_id else "ad_hoc",
        "name": workflow["name"],
        "definition": deepcopy(workflow),
        "task_count": len(tasks),
        "completed_task_count": len(tasks),
        "status": "succeeded",
        "error": None,
        "created_at": now,
        "updated_at": now,
        "tasks": tasks,
    }
    _instances[instance_id] = instance
    return {key: deepcopy(value) for key, value in instance.items() if key != "tasks"}


@app.post("/api/v1/instances")
async def create_ad_hoc_instance(workflow: dict[str, Any]) -> dict[str, Any]:
    return await _create_instance(workflow)


@app.post("/api/v1/templates/{template_id}/instances")
async def create_template_instance(template_id: str) -> dict[str, Any]:
    record = await get_template(template_id)
    return await _create_instance(record["definition"], template_id=template_id)


@app.get("/api/v1/instances")
async def list_instances(limit: int = 50, cursor: str | None = None) -> dict[str, Any]:
    del cursor
    items = []
    for instance in sorted(
        _instances.values(),
        key=lambda item: (item["created_at"], item["id"]),
        reverse=True,
    )[:limit]:
        items.append(
            {
                key: deepcopy(value)
                for key, value in instance.items()
                if key not in {"definition", "tasks"}
            }
        )
    return {"items": items, "next_cursor": None}


@app.get("/api/v1/instances/{instance_id}")
async def get_instance(instance_id: str) -> dict[str, Any]:
    return {
        key: deepcopy(value)
        for key, value in _instances[instance_id].items()
        if key != "tasks"
    }


@app.get("/api/v1/instances/{instance_id}/tasks")
async def get_tasks(instance_id: str) -> list[dict[str, Any]]:
    return deepcopy(_instances[instance_id]["tasks"])


@app.post("/api/v1/instances/{instance_id}/cancel")
async def cancel(instance_id: str) -> dict[str, Any]:
    _instances[instance_id]["status"] = "cancelled"
    return await get_instance(instance_id)
