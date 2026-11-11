"""Remove only reproducible development artifacts, never runtime or source data."""

import argparse
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = (
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".coverage",
    "htmlcov",
    "test-results",
    "playwright-report",
    "dist",
    "build",
    "firmware/build",
    "firmware/sdkconfig",
    "firmware/sdkconfig.old",
    ".DS_Store",
)
DEVELOPMENT_DEPENDENCIES = ("node_modules", ".browser-cache")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--include-dev-deps", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    names = ARTIFACTS + (DEVELOPMENT_DEPENDENCIES if args.include_dev_deps else ())
    targets = [ROOT / name for name in names]
    # Deliberately never walk data/, .secrets/, .venv/ or .git/.
    for name in ("src", "tests", "tools"):
        directory = ROOT / name
        if not directory.is_dir() or directory.is_symlink():
            continue
        targets.extend(directory.rglob("__pycache__"))
        targets.extend(directory.rglob(".DS_Store"))
    for path in sorted(set(targets)):
        if not path.exists() and not path.is_symlink():
            continue
        print(f"{'Would remove' if args.dry_run else 'Removing'} {path.relative_to(ROOT)}")
        if args.dry_run:
            continue
        if path.is_symlink() or path.is_file():
            path.unlink()
        else:
            shutil.rmtree(path)


if __name__ == "__main__":
    main()
