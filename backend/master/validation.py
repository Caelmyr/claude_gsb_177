"""Field-level validation for job submissions and job templates.

Validation happens in two stages so callers can react differently:

* **structural** checks are pure — required fields, numeric ranges, allowed
  enum values and mapper-specific parameter requirements.  Every problem is an
  ``error`` tagged with the exact offending field (e.g. ``params.pattern``), so
  the UI can highlight the right input.  Errors block saving/submitting.
* **environment** checks compare a candidate payload against the live cluster:
  registered worker capacity and the synthetic input data a mapper expects.
  Problems here are ``warnings`` (the cluster may simply be mid-bootstrap) and
  never block, but are surfaced prominently at template-apply / submit time.

Both stages return :class:`ValidationResult`; the same result shape is used by
the REST API and by direct callers such as ``JobManager.submit``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from backend.tasks.registry import has_mapper, has_reducer
from backend.tasks.samples import input_kind_for

# Field bounds, kept in one place so the API, the UI and persisted defaults all
# agree on what a legal job looks like.
LIMITS: dict[str, tuple[int, int]] = {
    "num_map_tasks": (1, 1000),
    "num_reduce_tasks": (1, 500),
    "input_rows": (10, 10_000_000),
}

# Mappers that cannot run without an extra parameter — ``field`` is a dotted
# path into the job payload (everything custom lives under ``params``).
REQUIRED_PARAMS: dict[str, tuple[tuple[str, str], ...]] = {
    "grep_mapper": (("params.pattern", "Grep 需要检索关键词 pattern (required keyword)"),),
}


@dataclass
class Issue:
    field: str
    message: str

    def to_dict(self) -> dict:
        return {"field": self.field, "message": self.message}


@dataclass
class ValidationResult:
    errors: list[Issue] = field(default_factory=list)
    warnings: list[Issue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def add_error(self, field_name: str, message: str) -> None:
        self.errors.append(Issue(field_name, message))

    def add_warning(self, field_name: str, message: str) -> None:
        self.warnings.append(Issue(field_name, message))

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "errors": [i.to_dict() for i in self.errors],
            "warnings": [i.to_dict() for i in self.warnings],
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _get_dotted(payload: dict, path: str) -> tuple[bool, Any]:
    """Fetch ``params.pattern`` style paths; returns (found, value)."""
    cur: Any = payload
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return False, None
    return True, cur


def _coerce_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


# ---------------------------------------------------------------------------
# Structural validation (pure; no cluster access)
# ---------------------------------------------------------------------------
def validate_structure(
    payload: dict,
    *,
    require_name: bool = True,
) -> ValidationResult:
    """Validate field presence, types and ranges of a job/template payload."""
    result = ValidationResult()

    # --- name ---------------------------------------------------------
    name = payload.get("name")
    if require_name:
        if name is None or not str(name).strip():
            result.add_error("name", "作业名称必填 Job name is required")
    elif name is not None and not str(name).strip():
        result.add_error("name", "作业名称不能为空 Job name cannot be empty")
    if name is not None and len(str(name)) > 120:
        result.add_error("name", "名称最多 120 个字符 Name must be at most 120 characters")

    # --- mapper / reducer enums --------------------------------------
    mapper = str(payload.get("mapper") or "")
    reducer = str(payload.get("reducer") or "")
    if not mapper:
        result.add_error("mapper", "请选择 Map 函数 Mapper is required")
    elif not has_mapper(mapper):
        result.add_error("mapper", f"未知的 Map 函数 Unknown mapper: {mapper}")
    if not reducer:
        result.add_error("reducer", "请选择 Reduce 函数 Reducer is required")
    elif not has_reducer(reducer):
        result.add_error("reducer", f"未知的 Reduce 函数 Unknown reducer: {reducer}")

    # --- numeric ranges ----------------------------------------------
    ints: dict[str, int] = {}
    for field_name, (lo, hi) in LIMITS.items():
        raw = payload.get(field_name)
        if raw is None or raw == "":
            result.add_error(field_name,
                             f"{field_name} 必填 {field_name} is required")
            continue
        value = _coerce_int(raw)
        if value is None:
            result.add_error(field_name,
                             f"{field_name} 必须是整数 must be an integer")
            continue
        ints[field_name] = value
        if value < lo or value > hi:
            result.add_error(
                field_name,
                f"{field_name} 超出范围 must be between {lo} and {hi} (got {value})",
            )

    # Map tasks should not vastly exceed the input: each map task would be
    # empty.  The planner already clamps this at runtime, but flag it early so
    # the user understands why the effective count changes.
    if "num_map_tasks" in ints and "input_rows" in ints:
        if ints["num_map_tasks"] > ints["input_rows"]:
            result.add_warning(
                "num_map_tasks",
                f"Map 任务数 ({ints['num_map_tasks']}) 多于输入行数 "
                f"({ints['input_rows']})，实际将被压缩到输入行数 "
                "(clamped to input rows at submit time)",
            )

    # --- params dict shape -------------------------------------------
    params = payload.get("params")
    if params is not None and not isinstance(params, dict):
        result.add_error("params", "params 必须是键值对象 must be an object")
        params = {}

    # --- mapper-specific required params -----------------------------
    if mapper and has_mapper(mapper):
        for path, message in REQUIRED_PARAMS.get(mapper, ()):
            found, value = _get_dotted(payload, path)
            if not found or value is None or (isinstance(value, str) and not value.strip()):
                result.add_error(path, message)

    return result


# ---------------------------------------------------------------------------
# Environment validation (cluster-aware; produces warnings only)
# ---------------------------------------------------------------------------
def validate_environment(payload: dict, registry, config) -> ValidationResult:
    """Compare a payload with the live cluster: worker capacity and input data.

    ``registry`` is the master's :class:`WorkerRegistry`; ``config`` is the
    validated :class:`ClusterConfig`.  Only warnings are produced here because
    resource availability changes over time — a busy/empty cluster is not an
    illegal request.
    """
    result = ValidationResult()

    alive = registry.alive() if registry is not None else []
    if not alive:
        result.add_warning(
            "cluster",
            "当前没有存活 Worker：作业会排队等待节点注册 "
            "(no alive workers; the job will stay pending)",
        )
    else:
        total_cores = sum(max(1, w.cpu_cores) for w in alive)
        num_map = _coerce_int(payload.get("num_map_tasks")) or 0
        # Parallelism the scheduler will actually drive, using the same factor
        # the rest of the system uses.
        capacity = max(len(alive), int(total_cores * config.map_parallelism_factor))
        if num_map > capacity * 4:
            result.add_warning(
                "num_map_tasks",
                f"Map 任务数 {num_map} 远超当前集群并发容量约 {capacity}"
                f"（{len(alive)} 个存活 Worker），周转会明显变慢 "
                "(far above current cluster capacity)",
            )

    # --- input data compatibility ------------------------------------
    mapper = str(payload.get("mapper") or "")
    if mapper and has_mapper(mapper):
        expected_kind = input_kind_for(mapper)
        params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
        kind_override = params.get("input_kind")
        if kind_override and kind_override != expected_kind:
            result.add_warning(
                "params.input_kind",
                f"模板指定的输入数据类型 {kind_override!r} 与 {mapper} 需要的 "
                f"{expected_kind!r} 不一致 (input data kind mismatch)",
            )

    return result


def validate_payload(payload: dict, registry=None, config=None, *,
                     require_name: bool = True) -> ValidationResult:
    """Run both stages; structural errors plus environment warnings."""
    result = validate_structure(payload, require_name=require_name)
    if registry is not None and config is not None:
        for issue in validate_environment(payload, registry, config).warnings:
            result.add_warning(issue.field, issue.message)
    return result
