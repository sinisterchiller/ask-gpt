"""Allow running the server as: python -m server"""
import sys
import traceback
from server.main import main

try:
    import asyncio
    asyncio.run(main())
except Exception as e:
    print(f"Fatal error: {e}", file=sys.stderr)
    traceback.print_exc()
    sys.exit(1)
