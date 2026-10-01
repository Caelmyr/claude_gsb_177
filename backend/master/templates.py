"""Named job-template store: create / duplicate / edit / delete / apply.

Templates live under ``data/templates/<template_id>.json`` as independent
documents.  Applying a template renders a fresh job-submission payload — it
never mutates the template — so users can freely tweak a form after applying
without risking the saved configuration.  Deleting a template likewise only
removes the template document; jobs already submitted from it are untouched.

All mutating operations go through one in-process lock plus the storage
layer's atomic, flock-guarded writes, and each method runs the same field
validation the submit endpoint uses, returning structured field-level errors
instead of raising on bad input.
"""

from __future__ import annotations

import threading
from typing import Optional

from backend.common.jsonutil import now_ms
from backend.common.models import JobTemplate, new_template
from backend.common.storage import Storage, list_files, read_json
from backend.master.validation import ValidationResult, validate_payload


# Fields that make up a template's reusable job configuration.
PAYLOAD_FIELDS = (
    "name", "mapper", "reducer",
    "num_map_tasks", "num_reduce_tasks", "input_rows", "params",
)


class TemplateError(ValueError):
    """Raised for not-found / name-conflict conditions."""


class TemplateManager:
    def __init__(self, storage: Storage) -> None:
        self.storage = storage
        self._templates: dict[str, JobTemplate] = {}
        self._lock = threading.RLock()
        self._load()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def _load(self) -> None:
        for path in list_files(self.storage.path("templates"), suffix=".json"):
            doc = read_json(path)
            if doc:
                tpl = JobTemplate.from_dict(doc)
                self._templates[tpl.template_id] = tpl

    def _save(self, tpl: JobTemplate) -> None:
        tpl.updated_ms = now_ms()
        self.storage.write(tpl.to_dict(), "templates", f"{tpl.template_id}.json")

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------
    def _validate(self, payload: dict) -> ValidationResult:
        # A template's own "name" is the template label, but the payload also
        # carries the job name suggested at apply time.
        return validate_payload(payload, require_name=True)

    def _find_by_name(self, name: str, exclude_id: str = "") -> Optional[JobTemplate]:
        wanted = name.strip().casefold()
        for tpl in self._templates.values():
            if tpl.template_id != exclude_id and tpl.name.strip().casefold() == wanted:
                return tpl
        return None

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------
    def list_templates(self) -> list[JobTemplate]:
        with self._lock:
            return sorted(self._templates.values(), key=lambda t: t.updated_ms, reverse=True)

    def get(self, template_id: str) -> Optional[JobTemplate]:
        with self._lock:
            return self._templates.get(template_id)

    def summary(self, tpl: JobTemplate) -> dict:
        d = tpl.to_dict()
        d.pop("version", None)
        return d

    def list_views(self) -> list[dict]:
        return [self.summary(t) for t in self.list_templates()]

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------
    def _build_payload(self, body: dict) -> dict:
        payload = {key: body.get(key) for key in PAYLOAD_FIELDS}
        payload["name"] = str(body.get("name") or "").strip()
        payload["params"] = dict(body.get("params") or {})
        return payload

    def create(self, body: dict) -> JobTemplate:
        """Validate and persist a brand-new template (409 on name clash)."""
        payload = self._build_payload(body)
        result = self._validate(payload)
        with self._lock:
            name = payload["name"]
            if name and self._find_by_name(name):
                result.add_error("name", f"已存在同名模板 Template named {name!r} already exists")
            if not result.ok:
                raise TemplateValidationError(result)

            tpl = new_template(
                name=name,
                mapper=payload["mapper"],
                reducer=payload["reducer"],
                num_map_tasks=int(payload["num_map_tasks"]),
                num_reduce_tasks=int(payload["num_reduce_tasks"]),
                input_rows=int(payload["input_rows"]),
                params=payload["params"],
                description=str(body.get("description") or ""),
            )
            self._templates[tpl.template_id] = tpl
            self._save(tpl)
        return tpl

    def update(self, template_id: str, body: dict) -> JobTemplate:
        """Replace the editable fields of an existing template."""
        payload = self._build_payload(body)
        result = self._validate(payload)
        with self._lock:
            tpl = self._templates.get(template_id)
            if tpl is None:
                raise TemplateError(f"unknown template: {template_id}")
            if self._find_by_name(payload["name"], exclude_id=template_id):
                result.add_error("name",
                                 f"已存在同名模板 Template named {payload['name']!r} already exists")
            if not result.ok:
                raise TemplateValidationError(result)

            tpl.name = payload["name"]
            tpl.mapper = payload["mapper"]
            tpl.reducer = payload["reducer"]
            tpl.num_map_tasks = int(payload["num_map_tasks"])
            tpl.num_reduce_tasks = int(payload["num_reduce_tasks"])
            tpl.input_rows = int(payload["input_rows"])
            tpl.params = payload["params"]
            tpl.description = str(body.get("description") or "")
            self._save(tpl)
        return tpl

    def duplicate(self, template_id: str, body: Optional[dict] = None) -> JobTemplate:
        """Copy a template into a new named one, optionally overriding fields.

        The source template is never modified; the copy records its origin in
        ``source_template_id``.
        """
        body = body or {}
        with self._lock:
            source = self._templates.get(template_id)
            if source is None:
                raise TemplateError(f"unknown template: {template_id}")

            merged = source.job_payload()
            merged["name"] = str(body.get("name") or f"{source.name} (copy)").strip()
            for key in ("mapper", "reducer", "num_map_tasks", "num_reduce_tasks", "input_rows"):
                if body.get(key) is not None:
                    merged[key] = body[key]
            if isinstance(body.get("params"), dict):
                merged["params"].update(body["params"])
            description = str(body.get("description") or f"复制自 {source.name}")

            result = self._validate(merged)
            if self._find_by_name(merged["name"]):
                result.add_error("name",
                                 f"已存在同名模板 Template named {merged['name']!r} already exists")
            if not result.ok:
                raise TemplateValidationError(result)

            tpl = new_template(
                name=merged["name"],
                mapper=merged["mapper"],
                reducer=merged["reducer"],
                num_map_tasks=int(merged["num_map_tasks"]),
                num_reduce_tasks=int(merged["num_reduce_tasks"]),
                input_rows=int(merged["input_rows"]),
                params=merged["params"],
                description=description,
                source_template_id=source.template_id,
            )
            self._templates[tpl.template_id] = tpl
            self._save(tpl)
        return tpl

    def delete(self, template_id: str) -> bool:
        """Remove a template.  Already-submitted jobs are unaffected."""
        with self._lock:
            if template_id not in self._templates:
                return False
            del self._templates[template_id]
            self.storage.delete("templates", f"{template_id}.json")
            return True

    # ------------------------------------------------------------------
    # Apply (read-only with respect to the template)
    # ------------------------------------------------------------------
    def apply(self, template_id: str, overrides: Optional[dict] = None,
              registry=None, config=None) -> dict:
        """Render a template into a job payload plus field-level validation.

        Returns ``{"payload": ..., "validation": {...}}``.  ``overrides``
        (typically fields the user changed in the form) are merged into a copy
        only — the stored template stays byte-for-byte identical.
        """
        with self._lock:
            tpl = self._templates.get(template_id)
            if tpl is None:
                raise TemplateError(f"unknown template: {template_id}")
            payload = tpl.job_payload(overrides)

        validation = validate_payload(payload, registry=registry, config=config)
        return {"payload": payload, "validation": validation.to_dict()}


class TemplateValidationError(ValueError):
    """Carries a structured :class:`ValidationResult` for the HTTP layer."""

    def __init__(self, result: ValidationResult) -> None:
        super().__init__("; ".join(i.message for i in result.errors) or "invalid template")
        self.result = result
