"""Named, reusable job-template store.

Templates live under ``templates/<tpl_id>.json`` in the same atomic Storage as
every other piece of cluster state, so writes survive crashes and concurrent
masters.  The manager implements the full template lifecycle:

* :meth:`create` — save the current form as a new named template;
* :meth:`duplicate` — copy an existing template (deep copy of ``params``),
  tweak a few fields and store under a new name — the source never changes;
* :meth:`update` — edit a template in place;
* :meth:`delete` — remove one;
* :meth:`apply` — return an *independent* job spec snapshot.  Applying only
  reads: manual edits made after applying (or during a run) mutate the
  returned snapshot and can never bleed back into the stored template.

Static validation (required fields, ranges, mapper params) is enforced on
create/update, but environment checks are intentionally not — a template must
remain valid as workers come and go.
"""

from __future__ import annotations

import threading
from typing import Optional

from backend.common.jsonutil import now_ms
from backend.common.models import JobTemplate, new_job_template
from backend.common.storage import Storage, list_files, read_json
from backend.master import validation as V


class TemplateError(ValueError):
    """Base class for template-store errors (maps to HTTP 400)."""


class DuplicateTemplateName(TemplateError):
    """Another template already owns this name (HTTP 409)."""


class TemplateNotFound(TemplateError):
    """No template with that id exists (HTTP 404)."""


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
        self.storage.write(tpl.to_dict(), "templates", f"{tpl.template_id}.json")

    # ------------------------------------------------------------------
    # Internal helpers (callers hold the lock)
    # ------------------------------------------------------------------
    def _find_name(self, name: str, exclude_id: str = "") -> Optional[JobTemplate]:
        wanted = name.strip().casefold()
        for tpl in self._templates.values():
            if tpl.template_id != exclude_id and tpl.name.strip().casefold() == wanted:
                return tpl
        return None

    def _require_unique_name(self, name: str, exclude_id: str = "") -> None:
        name = (name or "").strip()
        if not name:
            raise TemplateError("模板名称为必填项 template name is required")
        clash = self._find_name(name, exclude_id)
        if clash is not None:
            raise DuplicateTemplateName(f"模板名称已存在 template name already exists: {name!r}")

    @staticmethod
    def _spec_from_payload(payload: dict) -> dict:
        """Normalise + statically validate a raw payload; raise on errors."""
        spec, issues = V.normalize_spec(payload or {})
        issues.extend(V.validate_spec(spec))
        errors = [i for i in issues if i.severity == V.ERROR]
        if errors:
            raise TemplateError("; ".join(f"{i.field or 'job'}: {i.message}" for i in errors))
        return spec

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------
    def create(self, payload: dict) -> JobTemplate:
        spec = self._spec_from_payload(payload)
        with self._lock:
            name = str(payload.get("template_name") or spec["name"]).strip()
            self._require_unique_name(name)
            tpl = new_job_template(
                name=name,
                mapper=spec["mapper"],
                reducer=spec["reducer"],
                num_map_tasks=spec["num_map_tasks"],
                num_reduce_tasks=spec["num_reduce_tasks"],
                input_rows=spec["input_rows"],
                description=str(payload.get("description") or "").strip(),
                params=spec["params"],
                builtin=bool(payload.get("builtin", False)),
            )
            self._templates[tpl.template_id] = tpl
            self._save(tpl)
            return tpl

    def get(self, template_id: str) -> Optional[JobTemplate]:
        with self._lock:
            return self._templates.get(template_id)

    def require(self, template_id: str) -> JobTemplate:
        tpl = self.get(template_id)
        if tpl is None:
            raise TemplateNotFound(f"未知模板 unknown template: {template_id}")
        return tpl

    def list_all(self) -> list[JobTemplate]:
        with self._lock:
            return sorted(self._templates.values(),
                          key=lambda t: (not t.builtin, t.name.casefold()))

    def update(self, template_id: str, payload: dict) -> JobTemplate:
        spec = self._spec_from_payload(payload)
        with self._lock:
            tpl = self.require(template_id)
            new_name = str(payload.get("template_name") or payload.get("name") or tpl.name).strip()
            self._require_unique_name(new_name, exclude_id=template_id)
            tpl.name = new_name
            tpl.mapper = spec["mapper"]
            tpl.reducer = spec["reducer"]
            tpl.num_map_tasks = spec["num_map_tasks"]
            tpl.num_reduce_tasks = spec["num_reduce_tasks"]
            tpl.input_rows = spec["input_rows"]
            tpl.params = spec["params"]
            if "description" in payload:
                tpl.description = str(payload.get("description") or "").strip()
            tpl.updated_ms = now_ms()
            self._save(tpl)
            return tpl

    def duplicate(self, source_id: str, payload: Optional[dict] = None) -> JobTemplate:
        """Copy ``source_id`` into a fresh template, applying any overrides.

        ``payload`` may contain any job-spec field plus ``template_name`` and
        ``description``; fields left out are taken verbatim from the source.
        The source template is never mutated.
        """
        payload = dict(payload or {})
        with self._lock:
            source = self.require(source_id)
            new_name = str(payload.pop("template_name", "") or f"{source.name} (copy)").strip()
            description = str(payload.pop("description", "") or source.description).strip()
            merged = source.job_spec()
            for key in ("name", "mapper", "reducer", "num_map_tasks",
                        "num_reduce_tasks", "input_rows", "params"):
                if key in payload:
                    merged[key] = payload[key]
            merged["template_name"] = new_name
            merged["description"] = description
            return self.create(merged)

    def delete(self, template_id: str) -> None:
        with self._lock:
            tpl = self.require(template_id)
            del self._templates[template_id]
            self.storage.delete("templates", f"{template_id}.json")

    def ensure_seeded(self) -> None:
        """Seed the two most common built-in templates on first boot."""
        with self._lock:
            if self._templates:
                return
            from backend.tasks.samples import SAMPLE_JOBS

            for sample in SAMPLE_JOBS[:2]:
                payload = dict(sample)
                payload["template_name"] = sample["name"]
                payload["description"] = sample.get("description", "")
                payload["builtin"] = True
                # create() re-acquires the RLock (same thread) — safe.
                self.create(payload)

    # ------------------------------------------------------------------
    # Apply
    # ------------------------------------------------------------------
    def apply(self, template_id: str) -> dict:
        """Return a detached job-spec snapshot (see :meth:`JobTemplate.job_spec`)."""
        return self.require(template_id).job_spec()

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------
    @staticmethod
    def to_dict(tpl: JobTemplate) -> dict:
        d = tpl.to_dict()
        d["spec"] = tpl.job_spec()
        return d
