"""Patch Google Cloud CLI's bundled Python dependencies during image build."""

from __future__ import annotations

import shutil
import zipfile
from pathlib import Path


SDK_LIB = Path("/usr/lib/google-cloud-sdk/platform/bundledpythonunix/lib")
PATCH_DIR = Path("/tmp/gcloud-patches")


def main() -> None:
    sites = sorted(SDK_LIB.glob("python*/site-packages"))
    if not sites:
        raise SystemExit("Google Cloud SDK site-packages directory not found")

    for site in sites:
        for name in ("msgpack", "setuptools", "pkg_resources"):
            for path in site.glob(f"{name}*"):
                if path.is_dir():
                    shutil.rmtree(path)

    for wheel in PATCH_DIR.glob("*.whl"):
        for site in sites:
            with zipfile.ZipFile(wheel) as archive:
                archive.extractall(site)

    for site in sites:
        for old in ("msgpack-1.1.2.dist-info", "setuptools-70.3.0.dist-info"):
            if (site / old).exists():
                raise SystemExit(
                    f"Unpatched Google Cloud SDK dependency remains: {site / old}"
                )


if __name__ == "__main__":
    main()
