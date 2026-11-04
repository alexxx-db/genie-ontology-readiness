"""Regression tests for the scoring-correctness review fixes (one per fix)."""

import os
import unittest
from unittest.mock import AsyncMock, patch

from server import sql_client
from server.assessment import probes
from server.assessment.compare import compare_snapshots
from server.assessment.scoring import _finalize
from server.workspace_filter import set_catalog_scope, set_workspace_filter


def _p(key, score, weight, available=True):
    return {"key": key, "name": key, "score": score, "weight": weight, "available": available, "gaps": []}


class OverallOverAvailableTest(unittest.TestCase):
    def test_unavailable_pillar_not_scored_as_zero(self):
        out = _finalize([_p("a", 80.0, 30), _p("b", 0.0, 70, available=False)])
        self.assertEqual(out["overall"]["score"], 80.0)
        self.assertEqual(out["overall"]["coverage"]["available_weight"], 30)
        self.assertEqual(out["overall"]["coverage"]["total_weight"], 100)


class GenieLookbackTest(unittest.IsolatedAsyncioTestCase):
    async def test_total_window_wider_than_active_window(self):
        sql = AsyncMock(return_value=[{"total": "3", "active_30d": "1"}])
        with patch.object(probes, "execute_sql", sql):
            await probes._genie_audit_counts()
        q = sql.await_args.args[0]
        self.assertIn("INTERVAL 90 DAYS", q)  # existence window
        self.assertIn("INTERVAL 30 DAYS", q)  # activity window


class AdoptionExcludesAppSpTest(unittest.IsolatedAsyncioTestCase):
    async def test_app_sp_activity_excluded_parameterized(self):
        sql = AsyncMock(side_effect=[[{"c": "5"}], [{"c": "50"}]])
        set_workspace_filter({"mode": "include", "workspace_ids": ["1"]})
        try:
            with patch.dict(os.environ, {"DATABRICKS_CLIENT_ID": "sp-app-id"}), \
                 patch.object(probes, "execute_sql", sql):
                await probes.probe_adoption()
        finally:
            set_workspace_filter(None)
        users, queries = (c for c in sql.await_args_list)
        self.assertIn("COALESCE(user_identity.email, '') <> :app_sp", users.args[0])
        self.assertIn("COALESCE(executed_by, '') <> :app_sp", queries.args[0])
        self.assertNotIn("sp-app-id", users.args[0])
        self.assertEqual(users.kwargs["parameters"]["app_sp"], "sp-app-id")


class SystemModeInternalFilterTest(unittest.IsolatedAsyncioTestCase):
    async def test_headline_counts_exclude_internal_catalogs(self):
        sql = AsyncMock(return_value=[{"total": "10", "managed": "5"}])
        token = probes._request_sources.set({"system_ok": True, "catalogs": ["main"], "schemas": None})
        try:
            with patch.object(probes, "execute_sql", sql):
                await probes.probe_uc_foundation()
        finally:
            probes._request_sources.reset(token)
        schemas_q, tables_q = sql.await_args_list[0].args[0], sql.await_args_list[1].args[0]
        self.assertIn("catalog_name NOT IN (", schemas_q)
        self.assertIn("table_catalog NOT IN (", tables_q)


class NotEnabledClassificationTest(unittest.TestCase):
    def test_system_not_found_is_not_enabled(self):
        err = Exception("[TABLE_OR_VIEW_NOT_FOUND] The table or view `system`.`query`.`history` cannot be found.")
        self.assertEqual(probes._reason_for(err), "not_enabled")
        # An explicit denial still reads as a permission gap.
        self.assertEqual(probes._reason_for(Exception("PERMISSION_DENIED on system.access.audit")),
                         "insufficient_permission")


class CompareScopeMismatchTest(unittest.TestCase):
    def test_differing_scope_is_flagged(self):
        def snap(sid, scope):
            return {"id": sid, "scorecard": {"overall": {"score": 50}, "pillars": [], "scope": scope}}
        base = {"catalogs": ["a"], "schemas": [], "workspace_filter": None, "identity_mode": "obo"}
        cur = {**base, "catalogs": ["a", "b"], "identity_mode": "sp"}
        out = compare_snapshots(snap(1, base), snap(2, cur))
        self.assertTrue(out["scope_mismatch"])
        self.assertEqual(out["scope_mismatch_fields"], ["catalogs", "identity_mode"])
        self.assertFalse(compare_snapshots(snap(1, base), snap(2, dict(base)))["scope_mismatch"])


class UserScopeNoSpFallbackTest(unittest.IsolatedAsyncioTestCase):
    async def test_execute_sql_does_not_fall_back_when_disallowed(self):
        once = AsyncMock(side_effect=RuntimeError("PERMISSION_DENIED"))
        with patch.object(sql_client, "get_user_token", return_value="viewer"), \
             patch.object(sql_client, "FORCE_SP", False), \
             patch.object(sql_client, "_execute_once", once):
            with self.assertRaises(RuntimeError):
                await sql_client.execute_sql("SELECT 1", allow_sp_fallback=False)
        self.assertEqual(once.await_count, 1)  # never retried as the SP

    async def test_denied_user_chosen_catalog_is_dropped(self):
        calls = []

        async def fake_sql(q, *a, **kw):
            calls.append(kw)
            raise RuntimeError("PERMISSION_DENIED")

        set_catalog_scope(["secret"])
        try:
            with patch.object(probes, "execute_sql", fake_sql):
                s = await probes._do_resolve_sources()
        finally:
            set_catalog_scope(None)
        self.assertEqual(s["catalogs"], [])
        self.assertIn({"allow_sp_fallback": False}, calls)


if __name__ == "__main__":
    unittest.main()
