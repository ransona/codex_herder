from __future__ import annotations

import sys

from .app import run


def main() -> int:
    return run()


def usage_main() -> int:
    from .usage_tracker import run_usage_tracker

    return run_usage_tracker()


if __name__ == "__main__":
    raise SystemExit(main())
