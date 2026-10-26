# Async collection

Any fetch can be `async def`. It is detected automatically and needs no view
changes:

```python
# quickstart_app/serializers.py
import asyncio


async def fetch_bands(keys, collected, context):
    await asyncio.sleep(0)  # your async driver call here
    return {t.isin: "A" for t in collected.get("ticker", {}).values()}


class OrderSerializer(BatchSerializer):
    ticker = CachedRelatedField(lookup="isin", error="Ticker '{value}' not found.")
    customer = CachedRelatedField(lookup="isin", error="Customer '{value}' not found.")
    band = CachedServiceField(fetch=fetch_bands, depends_on=("ticker",), read_only=True)
    volume = serializers.IntegerField(min_value=1)
    reference_id = serializers.CharField(max_length=64)

    class Meta:
        model = Order
        fields = ("id", "ticker", "customer", "band", "volume", "reference_id")
```

- **Mixed plans**: when any fetch is async, all fetches run inside one
  combined coroutine, bridged to the process-wide loop **once**, with one
  timeout. Sync fetches in a mixed plan run on worker threads.
- **Timeouts**: collection that does not finish in time is cancelled and the
  request gets a **504** (`BatchCollectionTimeout`). The limit is the
  `COLLECT_TIMEOUT` setting (default 10.0 s); a replacement collector
  overrides it with the `collect_timeout` class attribute. Any *other*
  exception collection raises propagates and becomes a **500**.
- **Sync stays free**: an all-sync plan runs inline on the request thread. No
  loop is ever started for it, and none exists at import time.
- **Loop lifecycle**: async work runs on one process-wide daemon thread
  (`drf-batch-loop`) with one event loop, created lazily on first use and
  reference-counted per request. Call `drf_batch.loop.shutdown()` to stop it
  (tests, long-running processes; it also runs at interpreter exit).
- **Database connections**: inside async fetches, run ORM calls through
  `drf_batch.loop.to_thread(...)` (or `BatchCollector.to_thread`). The call
  runs on a worker thread whose connection is **closed afterwards**, so pooled
  threads never hold connections. A synchronous ORM call made directly on the
  loop thread works but keeps that thread's connection open.
- **Transaction visibility**: worker-thread and async fetches use their own
  connections. They do **not** see uncommitted writes made earlier in the
  same request (for example under `ATOMIC_REQUESTS = True`). Update and
  delete targets are always fetched on the request thread, inside the
  request's transaction.
- **ASGI, honestly**: DRF's view dispatch is synchronous under WSGI *and*
  ASGI, so there is no running loop inside a viewset action in either case,
  and the bridge above is used in both. `drf_batch.loop.wait(coro, timeout)`
  is the await-directly path for genuinely async hosts.
