"""Regression tests for review fixes: PDF filename encoding, plan-save ownership,
and the short negative cache on the serving-endpoints lookup."""

import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from server.routes import _shared


class PdfFilenameTest(unittest.TestCase):
    def test_non_latin_title_yields_encodable_header(self):
        fake_pisa = MagicMock()
        fake_pisa.CreatePDF.return_value = MagicMock(err=0)
        mod = MagicMock(pisa=fake_pisa)
        with patch.dict("sys.modules", {"xhtml2pdf": mod, "xhtml2pdf.pisa": fake_pisa}):
            from server.pdf import render_pdf_response
            resp = render_pdf_response("<html></html>", "データ計画")
        cd = resp.headers["content-disposition"]
        cd.encode("latin-1")  # would raise before the fix
        self.assertIn("filename*=UTF-8''%E3%83%87", cd)


class PlanSaveOwnershipTest(unittest.IsolatedAsyncioTestCase):
    async def test_cannot_link_plan_to_someone_elses_snapshot(self):
        from server.routes import plan as plan_route
        save = AsyncMock(return_value=1)
        with patch.object(plan_route.snapshots, "_enabled", AsyncMock(return_value=True)), \
             patch.object(plan_route.snapshots, "get_snapshot", AsyncMock(return_value=None)), \
             patch.object(plan_route.plans, "save_plan", save):
            resp = await plan_route.plan_save(
                plan_route.PlanSaveRequest(snapshot_id=7, markdown="x"), principal="me@example.com")
        self.assertEqual(resp.status_code, 404)
        save.assert_not_called()


class ModelLookupFailureCacheTest(unittest.IsolatedAsyncioTestCase):
    async def test_failed_lookup_expires_after_failure_ttl(self):
        _shared._cache_clear()
        with patch("server.config.get_workspace_host", return_value=None), \
             patch("server.config.get_auth_headers", return_value={}):
            await _shared.list_available_models()
        ts, _ = _shared._response_cache["serving_models"]
        # Entry is back-dated so it lives _FAILURE_TTL, not CACHE_TTL.
        self.assertLessEqual(_shared.CACHE_TTL - (time.time() - ts), _shared._FAILURE_TTL + 1)
        _shared._cache_clear()


if __name__ == "__main__":
    unittest.main()
