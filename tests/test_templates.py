"""Tests for job templates: full lifecycle, field-level validation and HTTP API."""

import json
import shutil
import tempfile
import unittest

from backend.common.config import ClusterConfig
from backend.common.logbus import LogBus
from backend.common.models import JobTemplate
from backend.common.storage import Storage
from backend.master import validation as V
from backend.master.job_manager import JobManager
from backend.master.registry import WorkerRegistry
from backend.master.templates import (
    DuplicateTemplateName, TemplateManager, TemplateNotFound,
)

try:
    from backend.master.server import Master
    _HAS_FLASK = True
except ImportError:  # Flask not installed in this environment
    _HAS_FLASK = False

VALID = {
    "name": "wc", "mapper": "wordcount_mapper", "reducer": "count_reducer",
    "num_map_tasks": 4, "num_reduce_tasks": 2, "input_rows": 800, "params": {},
}


def _registry_with_workers(storage, n, cpu_cores=4):
    reg = WorkerRegistry(storage, ClusterConfig())
    for i in range(n):
        reg.register({
            "worker_id": f"worker-{i}", "name": f"w{i}", "host": "127.0.0.1",
            "port": 9000 + i, "cpu_cores": cpu_cores, "mem_total_mb": 1024,
        })
    return reg


class TestSpecValidation(unittest.TestCase):
    def test_valid_spec_has_no_issues(self):
        issues = V.validate_spec(VALID)
        self.assertEqual([i for i in issues if i.severity == V.ERROR], [])

    def test_required_fields_pin_the_exact_field(self):
        spec, issues = V.normalize_spec({"name": "", "mapper": "", "reducer": ""})
        issues.extend(V.validate_spec(spec))
        fields = {i.field for i in issues if i.severity == V.ERROR}
        self.assertIn("name", fields)
        self.assertIn("mapper", fields)
        self.assertIn("reducer", fields)

    def test_unknown_mapper_and_reducer(self):
        bad = dict(VALID, mapper="nope", reducer="also-nope")
        fields = {i.field for i in V.validate_spec(bad)}
        self.assertIn("mapper", fields)
        self.assertIn("reducer", fields)

    def test_numeric_ranges(self):
        for field, lo, hi, bad_value in (
            ("num_map_tasks", 1, 1000, 5000),
            ("num_reduce_tasks", 1, 500, 0),
            ("input_rows", 10, 10_000_000, 1),
        ):
            issues = V.validate_spec(dict(VALID, **{field: bad_value}))
            err = next(i for i in issues if i.field == field and i.severity == V.ERROR)
            self.assertIn(str(lo), err.message)
            self.assertIn(str(hi), err.message)

    def test_non_integer_values_are_coercion_errors(self):
        spec, issues = V.normalize_spec(dict(VALID, num_map_tasks="abc"))
        self.assertTrue(any(i.field == "num_map_tasks" and i.severity == V.ERROR
                            for i in issues + V.validate_spec(spec)))

    def test_grep_requires_pattern(self):
        bad = dict(VALID, mapper="grep_mapper", params={})
        self.assertIn("params.pattern",
                      {i.field for i in V.validate_spec(bad) if i.severity == V.ERROR})
        good = dict(VALID, mapper="grep_mapper", params={"pattern": "map"})
        self.assertFalse(any(i.severity == V.ERROR for i in V.validate_spec(good)))

    def test_more_map_tasks_than_rows_is_a_warning_not_error(self):
        spec = dict(VALID, num_map_tasks=100, input_rows=10)
        issues = V.validate_spec(spec)
        warn = next(i for i in issues if i.field == "num_map_tasks")
        self.assertEqual(warn.severity, V.WARNING)


class TestEnvironmentValidation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.storage = Storage(self.tmp)
        self.config = ClusterConfig()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_no_alive_workers_is_a_job_wide_error(self):
        reg = WorkerRegistry(self.storage, self.config)
        issues = V.validate_environment(VALID, reg, self.config)
        err = next(i for i in issues if i.severity == V.ERROR)
        self.assertEqual(err.field, "")
        self.assertTrue(err.message)

    def test_one_worker_with_many_map_tasks_warns_on_field(self):
        reg = _registry_with_workers(self.storage, 1, cpu_cores=1)
        issues = V.validate_environment(dict(VALID, num_map_tasks=200), reg, self.config)
        self.assertTrue(any(
            i.field == "num_map_tasks" and i.severity == V.WARNING for i in issues))

    def test_right_sized_spec_has_no_environment_issues(self):
        reg = _registry_with_workers(self.storage, 3, cpu_cores=4)
        issues = V.validate_environment(VALID, reg, self.config)
        self.assertEqual(issues, [])


class TestTemplateManager(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.storage = Storage(self.tmp)
        self.mgr = TemplateManager(self.storage)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_create_get_list_and_persist(self):
        tpl = self.mgr.create(dict(VALID, template_name="我的模板"))
        self.assertEqual(tpl.name, "我的模板")
        reloaded = TemplateManager(self.storage)
        self.assertEqual({t.template_id for t in reloaded.list_all()}, {tpl.template_id})

    def test_create_rejects_invalid_spec(self):
        with self.assertRaises(ValueError) as ctx:
            self.mgr.create({"template_name": "bad", "mapper": "", "reducer": ""})
        self.assertIn("mapper", str(ctx.exception))
        self.assertEqual(self.mgr.list_all(), [])

    def test_duplicate_name_rejected_case_insensitive(self):
        self.mgr.create(dict(VALID, template_name="Same Name"))
        with self.assertRaises(DuplicateTemplateName):
            self.mgr.create(dict(VALID, template_name="same name "))

    def test_update_changes_fields_and_persists(self):
        tpl = self.mgr.create(dict(VALID, template_name="t"))
        updated = self.mgr.update(tpl.template_id,
                                  dict(VALID, template_name="t", num_map_tasks=16))
        self.assertEqual(updated.num_map_tasks, 16)
        self.assertEqual(TemplateManager(self.storage).get(tpl.template_id).num_map_tasks, 16)

    def test_update_rename_conflict(self):
        a = self.mgr.create(dict(VALID, template_name="a"))
        self.mgr.create(dict(VALID, template_name="b", num_map_tasks=2))
        with self.assertRaises(DuplicateTemplateName):
            self.mgr.update(a.template_id, dict(VALID, template_name="b"))

    def test_duplicate_is_independent_deep_copy(self):
        src = self.mgr.create(dict(
            VALID, template_name="src", mapper="grep_mapper", params={"pattern": "map"}))
        copy = self.mgr.duplicate(src.template_id, {"template_name": "dst",
                                                    "num_map_tasks": 9})
        self.assertNotEqual(copy.template_id, src.template_id)
        self.assertEqual(copy.num_map_tasks, 9)
        self.assertEqual(copy.params["pattern"], "map")
        # tweak the copy — source (and its params) must stay untouched
        self.mgr.update(copy.template_id,
                        dict(VALID, template_name="dst", mapper="grep_mapper",
                             num_map_tasks=9, params={"pattern": "shuffle"}))
        src_again = self.mgr.get(src.template_id)
        self.assertEqual(src_again.num_map_tasks, VALID["num_map_tasks"])
        self.assertEqual(src_again.params, {"pattern": "map"})

    def test_duplicate_unknown_template(self):
        with self.assertRaises(TemplateNotFound):
            self.mgr.duplicate("tpl-missing", {"template_name": "x"})

    def test_delete_removes_file(self):
        tpl = self.mgr.create(dict(VALID, template_name="t"))
        self.mgr.delete(tpl.template_id)
        with self.assertRaises(TemplateNotFound):
            self.mgr.require(tpl.template_id)
        self.assertIsNone(self.storage.read("templates", f"{tpl.template_id}.json"))

    def test_apply_returns_snapshot_isolated_from_template(self):
        tpl = self.mgr.create(dict(VALID, template_name="t"))
        snap1 = self.mgr.apply(tpl.template_id)
        snap1["params"]["simulate_failure"] = True
        snap1["num_map_tasks"] = 99
        snap2 = self.mgr.apply(tpl.template_id)
        self.assertNotIn("simulate_failure", snap2["params"])
        self.assertEqual(snap2["num_map_tasks"], VALID["num_map_tasks"])

    def test_seed_only_when_store_empty(self):
        self.mgr.ensure_seeded()
        first = len(self.mgr.list_all())
        self.assertGreater(first, 0)
        # delete all then re-seed: repopulates
        for t in list(self.mgr.list_all()):
            self.mgr.delete(t.template_id)
        self.mgr.ensure_seeded()
        self.assertEqual(len(self.mgr.list_all()), first)

    def test_seed_does_not_overwrite_user_templates(self):
        self.mgr.create(dict(VALID, template_name="mine"))
        self.mgr.ensure_seeded()
        names = {t.name for t in self.mgr.list_all()}
        self.assertIn("mine", names)


@unittest.skipUnless(_HAS_FLASK, "Flask is required for HTTP API tests")
class TestTemplateHTTPAPI(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.master = Master(self.tmp, host="127.0.0.1", port=0)
        self.client = self.master.app.test_client()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _post(self, path, body):
        return self.client.post(path, data=json.dumps(body),
                                content_type="application/json")

    def test_list_includes_seed_templates(self):
        resp = self.client.get("/api/templates")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["templates"])

    def test_full_lifecycle_over_http(self):
        # create
        r = self._post("/api/templates", dict(VALID, template_name="http-tpl"))
        self.assertEqual(r.status_code, 201, r.get_json())
        tpl = r.get_json()
        tid = tpl["template_id"]

        # apply returns a snapshot
        r = self.client.post(f"/api/templates/{tid}/apply")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["spec"]["mapper"], VALID["mapper"])

        # edit
        r = self.client.put(f"/api/templates/{tid}",
                            data=json.dumps(dict(VALID, template_name="http-tpl",
                                                 num_map_tasks=10)),
                            content_type="application/json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["num_map_tasks"], 10)

        # copy
        r = self._post(f"/api/templates/{tid}/copy", {"template_name": "http-tpl-2"})
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.get_json()["num_map_tasks"], 10)

        # delete
        r = self.client.delete(f"/api/templates/{tid}")
        self.assertEqual(r.status_code, 200)
        r = self.client.get(f"/api/templates/{tid}")
        self.assertEqual(r.status_code, 404)

    def test_create_duplicate_name_is_409(self):
        self._post("/api/templates", dict(VALID, template_name="dup"))
        r = self._post("/api/templates", dict(VALID, template_name="dup"))
        self.assertEqual(r.status_code, 409)

    def test_create_invalid_returns_400_with_field_error(self):
        r = self._post("/api/templates",
                       {"template_name": "bad", "mapper": "", "reducer": "",
                        "num_map_tasks": 99999, "input_rows": 1})
        self.assertEqual(r.status_code, 400)
        self.assertIn("mapper", r.get_json()["error"])

    def test_validate_endpoint_reports_errors_and_warnings(self):
        # no workers registered in this Master -> environment error
        r = self._post("/api/jobs/validate",
                       dict(VALID, num_map_tasks=99999, input_rows=10))
        data = r.get_json()
        self.assertFalse(data["valid"])
        err_fields = {e["field"] for e in data["errors"]}
        self.assertIn("num_map_tasks", err_fields)
        self.assertTrue(any(e["field"] == "" for e in data["errors"]))

    def test_submit_invalid_job_returns_field_locators(self):
        r = self._post("/api/jobs", {"name": "", "mapper": "x"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("errors", r.get_json())
        self.assertIn("name", {e["field"] for e in r.get_json()["errors"]})

    def test_submit_valid_job_still_runs_through_validation(self):
        # register one worker so the environment layer passes
        self._post("/api/workers/register", {
            "worker_id": "worker-0", "name": "w0", "host": "127.0.0.1", "port": 9001,
            "cpu_cores": 4, "mem_total_mb": 1024,
        })
        r = self._post("/api/jobs", VALID)
        self.assertEqual(r.status_code, 201, r.get_json())

    def test_apply_unknown_template_is_404(self):
        r = self.client.post("/api/templates/tpl-nope/apply")
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main()
