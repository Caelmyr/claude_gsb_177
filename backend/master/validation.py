"""Field-level validation for job specs and job templates.

Every validation problem is returned as an :class:`Issue` that pins the
offending field (``field`` — a dotted locator like ``params.pattern`` or the
sentinel ``""`` for a job-wide problem) so the UI can highlight exactly which
input is wrong instead of showing one opaque "submit failed" toast.

Two layers are provided:

* :func:`validate_spec` — **static** checks that depend only on the submitted
  values and the function registry (required fields, numeric ranges, mapper
  parameters).  This is the gate used both when saving a template and when
  submitting a job;
* :func:`validate_environment` — **dynamic** checks against the live cluster
  (available workers / resources) and input shape.  Problems here separate
  into ``error`` (cannot run at all right now, e.g. no alive worker) and
  ``warning`` (legal but likely a mistake, e.g. 200 map tasks with 1 worker).

Templates deliberately never run the environment layer: a template stored
today must stay usable next month when the cluster looks different.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from backend.tasks.registry import has_mapper, has_reducer

ERROR = "error"
WARNING = "warning"

# Hard numeric ranges shared by templates and submission.
NUM_MAP_RANGE = (1, 1000)
NUM_REDUCE_RANGE = (1, 500)
INPUT_ROWS_RANGE = (10, 10_000_000)
NAME_MAX_LEN = 120

# Mapper parameters. ``required`` params cause an error when missing/blank;
# ``optional`` params are only type-checked when present.
_MAPPER_PARAM_RULES: dict[str, dict[str, tuple[str, ...]]] = {
    "grep_mapper": {"required": ("pattern",), "optional": ()},
}

# Params that carry framework meaning regardless of the chosen mapper.
_BOOL_PARAMS = {"simulate_failure"}


@dataclass
class Issue:
    severity: str          # ERROR | WARNING
    field: str             # dotted locator, "" = whole job
    message: str

    def to_dict(self) -> dict:
        return {"severity": self.severity, "field": self.field, "message": self.message}


# ---------------------------------------------------------------------------
# Normalisation (parse + coerce raw form/JSON values)
# ---------------------------------------------------------------------------
def _coerce_int(value: Any) -> Optional[int]:
    """Return value as int, or None when it cannot be interpreted as one.

    Bools are rejected (``True`` is not a task count) and floats that carry a
    fractional part are rejected too — silently truncating user input would
    hide a typo.
    """
    if isinstance(value, bool) or value is None or value == "":
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str):
        s = value.strip()
        try:
            return int(s)
        except ValueError:
            try:
                f = float(s)
            except ValueError:
                return None
            return int(f) if f.is_integer() else None
    return None


def normalize_spec(payload: dict, defaults: Optional[dict] = None) -> tuple[dict, list[Issue]]:
    """Coerce a raw submission/template body into a typed job spec.

    The returned spec always contains all seven fields with proper types;
    values that could not be coerced get an ERROR issue and fall back to the
    defaults (or zero) so subsequent validation does not cascade into
    ``TypeError`` noise.
    """
    payload = payload or {}
    defaults = defaults or {}
    issues: list[Issue] = []

    name = payload.get("name")
    name = str(name).strip() if name is not None else ""

    mapper = str(payload.get("mapper") or "").strip()
    reducer = str(payload.get("reducer") or "").strip()

    def int_field(field: str, raw: Any, default: Any, lo: int, hi: int) -> int:
        coerced = _coerce_int(raw) if raw is not None else _coerce_int(default)
        if coerced is None:
            issues.append(Issue(ERROR, field, f"{field} 必须是整数 must be an integer"))
            return lo
        return coerced

    params_raw = payload.get("params", {})
    if not isinstance(params_raw, dict):
        issues.append(Issue(ERROR, "params", "params 必须是键值对象 must be an object"))
        params = {}
    else:
        params = dict(params_raw)

    spec = {
        "name": name,
        "mapper": mapper,
        "reducer": reducer,
        "num_map_tasks": int_field("num_map_tasks", payload.get("num_map_tasks"),
                                   defaults.get("num_map_tasks"), *NUM_MAP_RANGE),
        "num_reduce_tasks": int_field("num_reduce_tasks", payload.get("num_reduce_tasks"),
                                      defaults.get("num_reduce_tasks"), *NUM_REDUCE_RANGE),
        "input_rows": int_field("input_rows", payload.get("input_rows"),
                                defaults.get("input_rows"), *INPUT_ROWS_RANGE),
        "params": params,
    }
    return spec, issues


# ---------------------------------------------------------------------------
# Static validation
# ---------------------------------------------------------------------------
def validate_spec(spec: dict) -> list[Issue]:
    """Required-field / range / mapper-param checks. Works for templates too."""
    issues: list[Issue] = []

    if not spec.get("name"):
        issues.append(Issue(ERROR, "name", "作业名称为必填项 Job name is required"))
    elif len(spec["name"]) > NAME_MAX_LEN:
        issues.append(Issue(ERROR, "name",
                            f"名称最长 {NAME_MAX_LEN} 字符 name is too long (max {NAME_MAX_LEN})"))

    if not spec.get("mapper"):
        issues.append(Issue(ERROR, "mapper", "请选择 Map 函数 mapper is required"))
    elif not has_mapper(spec["mapper"]):
        issues.append(Issue(ERROR, "mapper", f"未知的 Map 函数 unknown mapper: {spec['mapper']}"))

    if not spec.get("reducer"):
        issues.append(Issue(ERROR, "reducer", "请选择 Reduce 函数 reducer is required"))
    elif not has_reducer(spec["reducer"]):
        issues.append(Issue(ERROR, "reducer", f"未知的 Reduce 函数 unknown reducer: {spec['reducer']}"))

    def range_check(field: str, lo: int, hi: int) -> None:
        value = spec.get(field)
        if isinstance(value, int) and not (lo <= value <= hi):
            issues.append(Issue(ERROR, field,
                                f"取值范围 {lo}–{hi}（当前 {value}） must be between {lo} and {hi}"))

    range_check("num_map_tasks", *NUM_MAP_RANGE)
    range_check("num_reduce_tasks", *NUM_REDUCE_RANGE)
    range_check("input_rows", *INPUT_ROWS_RANGE)

    # Map tasks beyond input rows is legal (the planner clamps) but wasteful.
    if isinstance(spec.get("num_map_tasks"), int) and isinstance(spec.get("input_rows"), int):
        if spec["num_map_tasks"] > spec["input_rows"]:
            issues.append(Issue(
                WARNING, "num_map_tasks",
                f"Map 任务数（{spec['num_map_tasks']}）多于输入行数"
                f"（{spec['input_rows']}），实际只会启动 {spec['input_rows']} 个；"
                "more map tasks than input rows — extra tasks are skipped",
            ))

    issues.extend(_validate_params(spec.get("params", {}), spec.get("mapper", "")))
    return issues


def _validate_params(params: dict, mapper: str) -> list[Issue]:
    issues: list[Issue] = []
    rules = _MAPPER_PARAM_RULES.get(mapper)
    if rules:
        for key in rules["required"]:
            value = params.get(key)
            if value is None or (isinstance(value, str) and not value.strip()):
                issues.append(Issue(ERROR, f"params.{key}",
                                    f"{mapper} 需要参数 {key!r} required parameter missing"))

    for key, value in params.items():
        if key in _BOOL_PARAMS and not isinstance(value, bool):
            issues.append(Issue(WARNING, f"params.{key}",
                                f"{key} 应为布尔值 expected a boolean, ignored if not"))
    return issues


# ---------------------------------------------------------------------------
# Environment / resource validation
# ---------------------------------------------------------------------------
def validate_environment(spec: dict, registry, config) -> list[Issue]:
    """Check the spec against the *current* cluster resources and input.

    ``registry`` is the live :class:`WorkerRegistry` and ``config`` the
    validated :class:`ClusterConfig`.
    """
    issues: list[Issue] = []
    alive = registry.alive()
    if not alive:
        issues.append(Issue(ERROR, "",
                            "当前没有存活的 Worker，作业将一直排队 no alive workers — job would stay pending"))
        return issues

    n_workers = len(alive)
    total_cores = sum(max(1, w.cpu_cores) for w in alive)

    def capacity(parallelism_factor: float) -> int:
        return max(1, int(total_cores * float(parallelism_factor)))

    map_cap = capacity(config.map_parallelism_factor)
    reduce_cap = capacity(config.reduce_parallelism_factor)

    num_map = spec.get("num_map_tasks", 0)
    num_reduce = spec.get("num_reduce_tasks", 0)
    if isinstance(num_map, int) and num_map > map_cap * 2:
        issues.append(Issue(
            WARNING, "num_map_tasks",
            f"Map 任务数 {num_map} 远超当前 {n_workers} 个节点（{total_cores} 核）的推荐并行度"
            f" ~{map_cap}，运行会明显变慢；far above recommended parallelism ~{map_cap}",
        ))
    if isinstance(num_reduce, int) and num_reduce > reduce_cap * 2:
        issues.append(Issue(
            WARNING, "num_reduce_tasks",
            f"Reduce 任务数 {num_reduce} 远超当前 {n_workers} 个节点（{total_cores} 核）的推荐并行度"
            f" ~{reduce_cap}；far above recommended parallelism ~{reduce_cap}",
        ))
    return issues


# ---------------------------------------------------------------------------
# Convenience
# ---------------------------------------------------------------------------
def validate_payload(payload: dict, defaults: Optional[dict] = None,
                     registry=None, config=None) -> tuple[dict, list[Issue]]:
    """Normalise + run static checks (and environment checks when given one).

    Returns the cleaned spec and the full issue list (coercion, static and
    environment) in field-locator order.
    """
    spec, issues = normalize_spec(payload, defaults)
    issues.extend(validate_spec(spec))
    if registry is not None and config is not None:
        issues.extend(validate_environment(spec, registry, config))
    return spec, issues


def has_errors(issues: list[Issue]) -> bool:
    return any(i.severity == ERROR for i in issues)


def issues_to_dicts(issues: list[Issue]) -> list[dict]:
    return [i.to_dict() for i in issues]
