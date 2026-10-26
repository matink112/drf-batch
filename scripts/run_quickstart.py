"""Run the README quickstart against a wheel, in a clean virtualenv.

Steps: build the wheel -> create a fresh venv -> install the wheel plus
Django and DRF -> copy ``scripts/quickstart`` to a temp dir -> run it with the
venv's interpreter (with ``PYTHONPATH`` cleared, so the source tree cannot
leak in).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = REPO_ROOT / "scripts" / "quickstart"


def _uv() -> str:
    found = shutil.which("uv")
    if found:
        return found
    fallback = Path.home() / ".local" / "bin" / "uv"
    if fallback.exists():
        return str(fallback)
    raise SystemExit("uv not found on PATH")


def _run(
    cmd: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None
) -> None:
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=cwd, env=env, check=True)


def _venv_python(venv: Path) -> Path:
    if os.name == "nt":  # pragma: no cover - POSIX dev/CI
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


def main() -> int:
    uv = _uv()
    version = f"{sys.version_info.major}.{sys.version_info.minor}"

    with tempfile.TemporaryDirectory(prefix="drf-batch-quickstart-") as raw_tmp:
        tmp = Path(raw_tmp)
        dist = tmp / "dist"
        _run([uv, "build", "--wheel", "--out-dir", str(dist)], cwd=REPO_ROOT)
        wheels = sorted(dist.glob("drf_batch-*.whl"))
        if not wheels:
            raise SystemExit("wheel was not built")
        wheel = wheels[-1]

        venv = tmp / "venv"
        _run([uv, "venv", str(venv), "--python", version])
        python = _venv_python(venv)
        _run(
            [
                uv,
                "pip",
                "install",
                "--python",
                str(python),
                str(wheel),
                "Django>=5.2",
                "djangorestframework>=3.15",
            ]
        )

        project = tmp / "project"
        shutil.copytree(TEMPLATE, project)

        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
            "PYTHONPATH": "",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        _run([str(python), "run.py"], cwd=project, env=env)

    print("quickstart passed against the built wheel in a clean venv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
