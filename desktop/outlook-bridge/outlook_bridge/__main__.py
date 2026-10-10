"""``python -m outlook_bridge``: see ``outlook_bridge.cli``.

The worker evaluates every message with the shared, pure reply semantics of the backend
(``suv_deals.domain``). If that package is not installed the worker refuses to start with a clear
message instead of correlating mail with a diverging copy.
"""

from __future__ import annotations

import sys

EXIT_RUNTIME = 5


def _main() -> int:
    try:
        from outlook_bridge.cli import main  # noqa: PLC0415 - guarded import with a clear message
    except ImportError as exc:
        if (exc.name or "").split(".")[0] != "suv_deals":
            raise
        sys.stderr.write(
            "outlook_bridge needs the shared reply semantics (package 'suv_deals', pure domain only);"
            " install it next to the worker - see desktop/outlook-bridge/README.md\n"
        )
        return EXIT_RUNTIME
    return main()


if __name__ == "__main__":
    sys.exit(_main())
