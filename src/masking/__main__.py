"""``python -m masking`` 入口，等价于 ``python -m masking.cli``。"""

from __future__ import annotations

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
