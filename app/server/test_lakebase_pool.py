"""Lakebase pool lifecycle: failed init is retried (rate-limited), a token refresh
never leaves ``_pool`` empty, and a table failure leaves history cleanly off."""

import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from server import lakebase_client as lc


def _fake_pool():
    pool = MagicMock()
    pool.close = AsyncMock()
    return pool


class LakebasePoolTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._saved = (lc._pool, lc._pool_created_at, lc._last_init_attempt)

    def tearDown(self):
        lc._pool, lc._pool_created_at, lc._last_init_attempt = self._saved

    async def test_failed_init_is_retried_after_backoff_only(self):
        lc._pool, lc._pool_created_at = None, 0.0
        lc._last_init_attempt = time.time()
        pool = _fake_pool()
        with patch.object(lc, "_create_pool", AsyncMock(return_value=pool)) as create, \
             patch("server.snapshots.ensure_table", AsyncMock()), \
             patch("server.plans.ensure_table", AsyncMock()):
            self.assertIsNone(await lc.get_pool())  # inside the backoff window
            create.assert_not_called()
            lc._last_init_attempt = time.time() - lc.INIT_RETRY_INTERVAL - 1
            self.assertIs(await lc.get_pool(), pool)

    async def test_refresh_swaps_new_pool_in_before_closing_old(self):
        old, new = _fake_pool(), _fake_pool()
        lc._pool = old
        lc._pool_created_at = time.time() - lc.TOKEN_REFRESH_INTERVAL - 1
        lc._last_init_attempt = 0.0
        with patch.object(lc, "_create_pool", AsyncMock(return_value=new)):
            self.assertIs(await lc.get_pool(), new)
        old.close.assert_awaited_once()

    async def test_failed_refresh_keeps_old_pool(self):
        old = _fake_pool()
        lc._pool = old
        lc._pool_created_at = time.time() - lc.TOKEN_REFRESH_INTERVAL - 1
        lc._last_init_attempt = 0.0
        with patch.object(lc, "_create_pool", AsyncMock(return_value=None)):
            self.assertIs(await lc.get_pool(), old)
        old.close.assert_not_awaited()

    async def test_table_failure_leaves_history_disabled(self):
        pool = _fake_pool()
        with patch.object(lc, "_create_pool", AsyncMock(return_value=pool)), \
             patch("server.snapshots.ensure_table", AsyncMock(side_effect=RuntimeError("perm"))):
            await lc.init_pool()  # must not raise
        self.assertFalse(lc.is_available())
        pool.close.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
