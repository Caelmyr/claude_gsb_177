"""Tests for job templates: CRUD, duplicate, apply isolation and validation."""

import shutil
import tempfile
import unittest

from backend.common.config import ClusterConfig
from backend.common.logbus import LogBus
from backend.common.storage import Storage
from backend.master.job_manager import JobManager, JobValidationError
from backend.master.templates import (
    TemplateError, TemplateManager, TemplateValidationError,
)
from backend.master.validation import validate_structure


VALID = {
    "name": "wc-standard",
    "mapper": "wordcount_mapper",
    "reducer": "count_reducer",
    "num_map_tasks": 8,
    "num_reduce_tasks": 4,
    "input_rows": 12000,
    "params": {},
}


class _FakeRegistry:
    """Minimal stand-in for WorkerRegistry.alive()."""

    def __init__(self, workers):
        self._workers = workers

    def alive(self):
        return self._workers


class _FakeWorker:
    def __init__(self, cpu_cores=4):
        self.cpu_cores = cpu_cores


class TestValidation(unittest.TestCase):
    def test_valid_payload(self):
        result = validate_structure(dict(VALID))
        self.assertTrue(result.ok, [i.message for i in result.errors])

    def test_required_fields_tagged(self):
        result = validate_structure({"name": "", "mapper": "", "reducer": ""})
        fields = {i.field for i in result.errors}
        self.assertIn("name", fields)
        self.assertIn("mapper", fields)
        self.assertIn("reducer", fields)
        self.assertIn("num_map_tasks", fields)
        self.assertIn("num_reduce_tasks", fields)
        self.assertIn("input_rows", fields)

    def test_numeric_range_locates_field(self):
        payload = dict(VALID, num_map_tasks=999999)
        result = validate_structure(payload)
        self.assertFalse(result.ok)
        self.assertEqual(result.errors[0].field, "num_map_tasks")
        self.assertIn("between", result.errors[0].message)

    def test_non_integer_locates_field(self):
        payload = dict(VALID, input_rows="abc")
        result = validate_structure(payload)
        self.assertEqual({i.field for i in result.errors}, {"input_rows"})

    def test_unknown_enum(self):
        payload = dict(VALID, mapper="nope_mapper")
        self.assertEqual(validate_structure(payload).errors[0].field, "mapper")

    def test_conditional_required_param(self):
        payload = dict(VALID, mapper="grep_mapper", params={})
        result = validate_structure(payload)
        self.assertFalse(result.ok)
        self.assertEqual(result.errors[0].field, "params.pattern")
        # providing it clears the error
        payload["params"] = {"pattern": "map"}
        self.assertTrue(validate_structure(payload).ok)

    def test_more_map_than_rows_is_warning_not_error(self):
        payload = dict(VALID, num_map_tasks=100, input_rows=30)
        result = validate_structure(payload)
        self.assertTrue(result.ok)
        self.assertEqual(result.warnings[0].field, "num_map_tasks")

    def test_environment_warning_no_workers(self):
        from backend.master.validation import validate_environment
        result = validate_environment(VALID, _FakeRegistry([]), ClusterConfig())
        self.assertTrue(result.warnings)
        self.assertEqual(result.warnings[0].field, "cluster")

    def test_environment_warning_input_kind_mismatch(self):
        from backend.master.validation import validate_environment
        payload = dict(VALID, mapper="kv_mapper",
                       params={"input_kind": "wordcount"})
        result = validate_environment(payload, _FakeRegistry([_FakeWorker()]),
                                      ClusterConfig())
        fields = {i.field for i in result.warnings}
        self.assertIn("params.input_kind", fields)


class TestTemplateCrud(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.storage = Storage(self.tmp)
        self.templates = TemplateManager(self.storage)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_create_persists_and_lists(self):
        tpl = self.templates.create(dict(VALID))
        again = TemplateManager(self.storage)  # fresh manager = reload from disk
        self.assertEqual([t.template_id for t in again.list_templates()], [tpl.template_id])

    def test_create_rejects_invalid_with_field_errors(self):
        bad = dict(VALID, num_map_tasks=-3, mapper="")
        with self.assertRaises(TemplateValidationError) as ctx:
            self.templates.create(bad)
        fields = {i.field for i in ctx.exception.result.errors}
        self.assertEqual(fields, {"mapper", "num_map_tasks"})

    def test_duplicate_names_rejected(self):
        self.templates.create(dict(VALID))
        with self.assertRaises(TemplateValidationError) as ctx:
            self.templates.create(dict(VALID))
        self.assertEqual(ctx.exception.result.errors[0].field, "name")

    def test_update_existing(self):
        tpl = self.templates.create(dict(VALID))
        updated = self.templates.update(tpl.template_id,
                                        dict(VALID, num_map_tasks=16, name="wc-standard"))
        self.assertEqual(updated.num_map_tasks, 16)
        # persisted
        self.assertEqual(self.templates.get(tpl.template_id).num_map_tasks, 16)

    def test_update_unknown_raises(self):
        with self.assertRaises(TemplateError):
            self.templates.update("tpl-nope", dict(VALID))

    def test_update_name_clash(self):
        self.templates.create(dict(VALID))
        other = self.templates.create(dict(VALID, name="other"))
        with self.assertRaises(TemplateValidationError):
            self.templates.update(other.template_id, dict(VALID, name="wc-standard"))

    def test_duplicate_copies_fields_independently(self):
        tpl = self.templates.create(dict(VALID))
        copy = self.templates.duplicate(tpl.template_id, {"name": "wc-copy"})
        self.assertNotEqual(copy.template_id, tpl.template_id)
        self.assertEqual(copy.source_template_id, tpl.template_id)
        self.assertEqual(copy.mapper, tpl.mapper)
        self.assertEqual(copy.num_reduce_tasks, tpl.num_reduce_tasks)
        # editing the copy leaves the source untouched
        self.templates.update(copy.template_id,
                              dict(VALID, name="wc-copy", num_map_tasks=2))
        self.assertEqual(self.templates.get(tpl.template_id).num_map_tasks, 8)

    def test_duplicate_with_overrides(self):
        tpl = self.templates.create(dict(VALID))
        copy = self.templates.duplicate(
            tpl.template_id, {"name": "wc-grep", "mapper": "grep_mapper",
                              "params": {"pattern": "shuffle"}})
        self.assertEqual(copy.mapper, "grep_mapper")
        self.assertEqual(copy.params["pattern"], "shuffle")

    def test_delete_removes_template_only(self):
        tpl = self.templates.create(dict(VALID))
        self.assertTrue(self.templates.delete(tpl.template_id))
        self.assertIsNone(self.templates.get(tpl.template_id))
        self.assertFalse(self.templates.delete(tpl.template_id))
        # delete survives a reload
        self.assertEqual(TemplateManager(self.storage).list_templates(), [])

    def test_apply_returns_independent_copy(self):
        tpl = self.templates.create(dict(VALID))
        result = self.templates.apply(tpl.template_id)
        payload = result["payload"]
        payload["num_map_tasks"] = 99
        payload["params"]["simulate_failure"] = True
        # stored template is unchanged
        fresh = self.templates.get(tpl.template_id)
        self.assertEqual(fresh.num_map_tasks, 8)
        self.assertNotIn("simulate_failure", fresh.params)

    def test_apply_with_overrides_does_not_mutate_template(self):
        tpl = self.templates.create(dict(VALID))
        result = self.templates.apply(tpl.template_id, {"input_rows": 999})
        self.assertEqual(result["payload"]["input_rows"], 999)
        self.assertEqual(self.templates.get(tpl.template_id).input_rows, 12000)

    def test_apply_validates_and_reports_warnings(self):
        tpl = self.templates.create(dict(VALID))
        result = self.templates.apply(
            tpl.template_id, registry=_FakeRegistry([]), config=ClusterConfig())
        self.assertTrue(result["validation"]["ok"])
        self.assertTrue(result["validation"]["warnings"])

    def test_apply_unknown(self):
        with self.assertRaises(TemplateError):
            self.templates.apply("tpl-nope")

    def test_persistence_survives_manager_reload(self):
        self.templates.create(dict(VALID, name="persisted",
                                   params={"pattern": "x"}))
        reloaded = TemplateManager(self.storage)
        tpl = reloaded.list_templates()[0]
        self.assertEqual(tpl.name, "persisted")
        self.assertEqual(tpl.params, {"pattern": "x"})


class TestTemplateSubmitIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.storage = Storage(self.tmp)
        self.templates = TemplateManager(self.storage)
        self.jobs = JobManager(self.storage, ClusterConfig(), LogBus(self.storage))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_submit_from_template_does_not_modify_it(self):
        tpl = self.templates.create(dict(VALID))
        result = self.templates.apply(tpl.template_id)
        payload = result["payload"]
        job = self.jobs.submit(dict(payload))
        self.assertEqual(job.mapper, "wordcount_mapper")
        # template still intact after a real submission
        self.assertEqual(self.templates.get(tpl.template_id).num_map_tasks, 8)

    def test_submit_invalid_payload_raises_structured_error(self):
        with self.assertRaises(JobValidationError) as ctx:
            self.jobs.submit({"name": "x", "mapper": "wordcount_mapper",
                              "reducer": "count_reducer",
                              "num_map_tasks": 0, "num_reduce_tasks": 1,
                              "input_rows": 100})
        self.assertEqual(ctx.exception.result.errors[0].field, "num_map_tasks")


if __name__ == "__main__":
    unittest.main()
