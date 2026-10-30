# Changelog

All notable changes to this project are documented in this file. Entries are
generated from [Conventional Commits](https://www.conventionalcommits.org/) by
`make bump`, and the project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## v0.3.0 (2026-10-30)

### BREAKING CHANGE

- read-only cached fields ignore the request body; a value
the row sends for them is no longer checked against the map (a miss used
to be a 400). Derive their keys with `derived_from` and reject rows in
`validate()` via `self.cached()`. Update/destroy now return 404/403
before collection runs.

### Feat

- **fields**: derive cached keys from the row or the stored target
- **mixins**: opt-in PUT batch_replace for full-row updates

### Fix

- **tests**: satisfy mypy and ruff format in CI

## v0.2.0 (2026-10-27)

### BREAKING CHANGE

- sync fetches run on worker threads outside a transaction
(on the request thread inside one), all-sync plans start the event loop,
and COLLECT_TIMEOUT applies to them; batch_concurrent_collection = False
restores sequential collection.

### Feat

- **collection**: run collection plans as a concurrent dependency graph
- **loop**: EVENT_LOOP/THREAD_LOOP, ASGI loop, use_loop(), run_sync()

## v0.1.0 (2026-10-26)

### Feat

- initial release
