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

Sync and async fetches run together as one dependency graph on an event loop
(see [Concurrency and `depends_on`](collection.md#concurrency-and-depends_on)):
`ticker` and `customer` start together, and `band` starts as soon as `ticker`
is done.

## Timeouts

Collection that does not finish within `COLLECT_TIMEOUT` (default 10.0 s) is
cancelled and the request gets a **504** (`BatchCollectionTimeout`). The
whole graph shares one timeout; a replacement collector overrides it with
its `collect_timeout` class attribute. Any *other* exception a fetch raises
propagates unchanged (the remaining fetches are cancelled) and becomes a
**500**.

```python
# settings.py
DRF_BATCH = {"COLLECT_TIMEOUT": 2.5}
```

!!! note
    An `async def` fetch is cancelled at its next `await`. A sync fetch
    already running on a thread cannot be interrupted: the request still gets
    its 504 on time, and the call finishes in the background.

## Which loop runs collection

DRF dispatches views synchronously under WSGI *and* ASGI, so the request
thread hands the collection graph to an event loop running in **another**
thread and waits for it. The `EVENT_LOOP` setting picks the loop:

| `EVENT_LOOP` | Under ASGI | Anywhere else |
|---|---|---|
| `"auto"` (default) | the **ASGI server's loop** | thread mode |
| `"thread"` | thread mode | thread mode |

**Thread mode** uses a loop your application already runs in its own thread
(registered with `use_loop()` or `THREAD_LOOP`, below) or, by default, the
built-in `drf-batch-loop` thread: one daemon thread with one event loop for
the process, created on first use (no thread exists at import time).

```python
# settings.py
DRF_BATCH = {
    "EVENT_LOOP": "auto",  # or "thread": never use the ASGI server's loop
}
```

### Under ASGI

A sync view under ASGI runs in an `asgiref` worker thread, which knows the
server loop that spawned it. With `"auto"`, collection runs **on that loop**,
the same one your async views and their clients use. Nothing to configure.

!!! warning "A blocking fetch stalls the whole server"
    On the server's loop, an `async def` fetch that blocks (a sync HTTP
    call, `time.sleep`, a sync ORM query) stops *every* request the server
    is handling. Choose `EVENT_LOOP = "thread"` to keep collection on its own
    loop if you cannot rule that out. See [Don't block the loop](#dont-block-the-loop).

### Your own loop, in your own thread

If the application already runs a loop in a background thread (with its
clients and pools created on it), register it and collection uses it in
thread mode. drf-batch never starts, stops or closes it.

```python
# quickstart_app/runtime.py
import asyncio
import threading

import aiohttp

io_loop = asyncio.new_event_loop()
threading.Thread(target=io_loop.run_forever, name="app-io", daemon=True).start()


async def _make_session() -> aiohttp.ClientSession:
    # A ClientSession must be created on the loop that will use it.
    return aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=2))


http = asyncio.run_coroutine_threadsafe(_make_session(), io_loop).result()
```

```python
# quickstart_app/apps.py
from django.apps import AppConfig


class QuickstartConfig(AppConfig):
    name = "quickstart_app"

    def ready(self):
        from drf_batch.loop import use_loop

        from .runtime import io_loop

        use_loop(io_loop)  # use_loop(None) goes back to the built-in thread
```

```python
# quickstart_app/serializers.py
from .runtime import http


async def fetch_bands(keys, collected, context):
    async with http.get("https://bands.example/v1", params={"isin": sorted(keys)}) as r:
        r.raise_for_status()
        return await r.json()
```

Or name a zero-argument callable that returns the loop in settings. A lazy
getter like this one also starts a fresh loop in each forked worker process:

```python
# quickstart_app/runtime.py
import asyncio
import os
import threading

_lock = threading.Lock()
_loop: asyncio.AbstractEventLoop | None = None
_pid = 0


def get_io_loop() -> asyncio.AbstractEventLoop:
    global _loop, _pid
    with _lock:
        if _loop is None or _pid != os.getpid():  # first use, or a forked child
            _loop, _pid = asyncio.new_event_loop(), os.getpid()
            threading.Thread(
                target=_loop.run_forever, name="app-io", daemon=True
            ).start()
    return _loop
```

```python
# settings.py
DRF_BATCH = {"THREAD_LOOP": "quickstart_app.runtime.get_io_loop"}
```

`use_loop()` takes precedence over `THREAD_LOOP`. A registered loop that is
not running is an `ImproperlyConfigured` on the first request, not a 504.
`drf_batch.loop.current_loop()` returns the loop collection would use from the
calling thread.

## Async clients

An async client (`aiohttp.ClientSession`, `httpx.AsyncClient`, an `asyncpg`
pool) is **bound to the loop it was created on**: using it from another loop
fails or hangs. Create it on the loop that runs your fetches, reuse it across
requests, and never share one across loops. A per-loop cache works in every
mode (ASGI server loop, your own loop, the built-in thread):

```python
# quickstart_app/clients.py
import asyncio

import httpx

_clients: dict[asyncio.AbstractEventLoop, httpx.AsyncClient] = {}


def client() -> httpx.AsyncClient:
    """The client for the running loop, created on first use."""
    loop = asyncio.get_running_loop()
    found = _clients.get(loop)
    if found is None or found.is_closed:
        found = _clients[loop] = httpx.AsyncClient(timeout=2.0)
    return found


async def close_clients() -> None:
    """Await on each loop at shutdown (e.g. from your own loop's thread)."""
    loop = asyncio.get_running_loop()
    if (found := _clients.pop(loop, None)) is not None:
        await found.aclose()
```

```python
# quickstart_app/serializers.py
from .clients import client


async def fetch_prices(keys, collected, context):
    response = await client().post("https://prices.example/v1/batch", json=sorted(keys))
    response.raise_for_status()
    return response.json()  # {isin: price}
```

!!! tip
    Do not create a client per fetch call (`async with httpx.AsyncClient()`
    inside the fetch): it works, but every batch pays for new connections
    and TLS handshakes.

## Don't block the loop

Everything that shares a loop waits while one coroutine blocks it: with the
built-in thread that is every batch request in the process, under ASGI the
whole server.

```python
import requests


# Bad: requests.get blocks the event loop thread.
async def fetch_bands(keys, collected, context):
    return requests.get(
        "https://bands.example/v1", params={"isin": sorted(keys)}
    ).json()


# Good: a plain sync fetch. The graph runs it on a worker thread for you.
def fetch_bands(keys, collected, context):
    return requests.get(
        "https://bands.example/v1", params={"isin": sorted(keys)}
    ).json()


# Good: blocking work inside an async fetch goes to a thread explicitly.
from drf_batch.loop import run_sync


async def fetch_bands(keys, collected, context):
    tickers = await run_sync(lambda: list(Ticker.objects.filter(isin__in=keys)))
    prices = await client().post("https://prices.example/v1/batch", json=sorted(keys))
    return {t.isin: prices.json()[t.isin] for t in tickers}
```

## Database connections

Django connections are per thread. Where a sync fetch (or a `run_sync(...)`
call) runs depends on the request's connection:

- **Not in a transaction**: on a worker thread, concurrently with the other
  fetches, on that thread's own connection.
- **In a transaction** (`ATOMIC_REQUESTS = True`, an outer `atomic()`, Django's
  `TestCase`): on the **request thread**, which runs these calls one at a
  time while it waits for the graph, so they see the transaction's
  uncommitted writes. Async fetches stay concurrent. The same applies when a
  database is a private in-memory SQLite database (`NAME = ":memory:"`),
  where every connection would be a separate, empty database.

```python
# Inside a transaction a worker thread could not see this row; the
# request thread runs the sync ticker fetch, so the batch resolves it.
with transaction.atomic():
    Ticker.objects.create(isin="1103", name="Gamma Corp")
    rows = [
        {"ticker": "1103", "customer": "112001", "volume": 1, "reference_id": "TX-1"}
    ]
    serializer = OrderSerializer(data=rows, many=True)
    serializer.is_valid(raise_exception=True)
```

Worker threads handle their connections like a request does
(`close_old_connections()` before and after each call): with the default
`CONN_MAX_AGE = 0` a connection is closed after the call; with a positive
value it is kept and reused by that worker thread until it expires or
breaks.

```python
# settings.py: reuse worker-thread connections for up to 60 s
DATABASES = {
    "default": {
        ...,
        "CONN_MAX_AGE": 60,
        "CONN_HEALTH_CHECKS": True,
    }
}
```

With persistent connections each worker thread of the loop's thread pool
(`min(32, os.cpu_count() + 4)` threads by default) may keep one connection
per database, so count them against the server's connection limit.

In your own code inside an async fetch or collector:

| Helper | Runs on | Sees the request's transaction |
|---|---|---|
| `drf_batch.loop.run_sync(func, ...)` / `BatchCollector.run_sync` | request thread inside a transaction, else a worker thread | yes |
| `drf_batch.loop.to_thread(func, ...)` / `BatchCollector.to_thread` | always a worker thread | no |

## Calling the serializer from async code

`is_valid()` is synchronous and must not run on an event loop thread (that is
a `RuntimeError`). From an async view or task, call it through
`sync_to_async`; with `EVENT_LOOP = "auto"` collection then runs on your loop:

```python
from asgiref.sync import sync_to_async


async def import_orders(rows):
    serializer = OrderSerializer(data=rows, many=True)
    await sync_to_async(serializer.is_valid)(raise_exception=True)
    await sync_to_async(serializer.save)()
```

## Loop lifecycle

- The built-in loop starts lazily on first use and keeps running between
  requests. `drf_batch.loop.shutdown()` stops it (tests, long-running
  processes); it also runs at interpreter exit, bounded to a few seconds.
- After `fork()` (gunicorn `--preload`, Celery prefork) the built-in loop is
  recreated in the child on first use. A loop you register yourself is not:
  start it lazily per process, as `get_io_loop()` above does.
- Sequential collection (`batch_concurrent_collection = False`, see
  [Concurrency](collection.md#turning-concurrency-off)) starts no loop at all
  unless a fetch is `async def`.
