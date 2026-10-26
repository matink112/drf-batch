# Contributing

`make help` lists the shortcuts (`make install`, `make check`,
`make docs-serve`, `make test-fast`, …). They wrap the commands below:

```bash
uv sync                 # dev environment (Python 3.11+)
nox -s all              # lint + typecheck + tests (coverage gate)
nox -s matrix           # oldest/newest Django x DRF cells, like CI
python scripts/run_quickstart.py   # README quickstart against the built wheel
nox -s docs             # build the docs site strictly
nox -s docs -- serve    # preview it at http://127.0.0.1:8000
```

- Tests run on SQLite by default; set `DRF_BATCH_TEST_DB=postgres` (plus the
  standard `PG*` variables) to run them against PostgreSQL.
- Every behavior change needs a test. User-facing changes also update `docs/`
  (the guides) and, where relevant, `README.md`.
- Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/)
  (`feat:`, `fix:`, `docs:`, …; `feat!:` for a breaking change). They decide
  the next version and become the `CHANGELOG.md` entries, so don't edit the
  changelog by hand. `make hooks` installs a hook that checks each message;
  `make commit` writes one interactively.

## Releasing

From a clean `main`:

```bash
make version        # current version and what the next one would be
make changelog      # preview the next CHANGELOG entry
make release        # run every check, then bump, commit and tag vX.Y.Z
make push-release   # push main and the tag; the tag publishes to PyPI
```

`make bump INCREMENT=MINOR` forces the increment, `PRERELEASE=rc` cuts a
release candidate, and `make bump-dry` shows the result without changing
anything.
- The public API is what `drf_batch/__init__.py` exports. The `pipeline`,
  `conflicts` and `writers` modules are internal.
