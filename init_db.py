"""Initialize all three databases (schema + sample data).

Each server defines its own ``User``/``Post`` classes (with different decorators
per path). ``SQLModel.metadata`` is process-global, so importing two servers in
one process collides. This script runs each server's ``init_db()`` in its own
subprocess to keep them isolated.

Running a server directly also inits its own DB on startup, so this script is
optional — it's useful when you want to inspect the seeded DB without starting
a server.

    python init_db.py
"""

import subprocess
import sys

SERVERS = ["fastmcp_handwritten", "nexusx_simple", "nexusx_usecase"]


def main() -> None:
    for server in SERVERS:
        print(f"[init] {server}")
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                f"import asyncio; import {server}; asyncio.run({server}.init_db())",
            ],
            check=False,
        )
        if result.returncode != 0:
            print(f"  ERROR: {server} init failed")
            sys.exit(1)

    print("\nAll databases ready. Now run any of:")
    for server in SERVERS:
        print(f"  python {server}.py             # stdio")
        print(f"  python {server}.py --http      # streamable-http")


if __name__ == "__main__":
    main()
