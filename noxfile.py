"""Nox sessions for drf-batch.

The environment is owned by uv (run ``uv sync`` first); nox only runs the
checks, so sessions skip their own venvs.
"""

from __future__ import annotations

import nox


@nox.session(venv_backend="none")
def lint(session: nox.Session) -> None:
    session.run("uv", "run", "ruff", "check")
    session.run("uv", "run", "ruff", "format", "--check")


@nox.session(venv_backend="none")
def typecheck(session: nox.Session) -> None:
    session.run("uv", "run", "mypy")


@nox.session(venv_backend="none")
def test(session: nox.Session) -> None:
    session.run("uv", "run", "pytest")


# (python, Django, DRF) cells mirroring .github/workflows/ci.yml.
MATRIX = [
    ("3.11", ">=5.2,<5.3", ">=3.15,<3.16"),
    ("3.13", ">=6.1,<6.2", ">=3.16"),
]


@nox.session(venv_backend="none")
def matrix(session: nox.Session) -> None:
    # ``--with`` layers the pins over the locked env; ``uv run`` alone would
    # re-sync to uv.lock and silently test the locked versions instead.
    for python, django, drf in MATRIX:
        session.run(
            "uv",
            "run",
            "--frozen",
            "--python",
            python,
            "--with",
            f"Django{django}",
            "--with",
            f"djangorestframework{drf}",
            "pytest",
            env={"DRF_BATCH_EXPECT_DJANGO": django, "DRF_BATCH_EXPECT_DRF": drf},
        )


@nox.session(venv_backend="none")
def docs(session: nox.Session) -> None:
    """Build the docs site strictly (``nox -s docs -- serve`` to preview)."""
    command = session.posargs[0] if session.posargs else "build"
    args = ["--strict"] if command == "build" else []
    session.run(
        "uv",
        "run",
        "--frozen",
        "--group",
        "docs",
        "mkdocs",
        command,
        *args,
        env={"NO_MKDOCS_2_WARNING": "1"},
    )


@nox.session(venv_backend="none")
def all(session: nox.Session) -> None:
    lint(session)
    typecheck(session)
    test(session)
