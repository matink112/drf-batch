# Changelog

All notable changes to this project are documented in this file. Entries are
generated from [Conventional Commits](https://www.conventionalcommits.org/) by
`make bump`, and the project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
