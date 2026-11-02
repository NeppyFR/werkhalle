"""`python -m werkhalle.mes`. Pool workers re-import this file under the name `__mp_main__`, so it must do nothing on import."""
from __future__ import annotations

import sys


def main() -> int:
    from .server import main as server_main

    return server_main()


if __name__ == "__main__":
    sys.exit(main())
