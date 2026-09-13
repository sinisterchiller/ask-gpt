"""Allow running the server as: python -m server"""
import sys
import traceback
try:
    import asyncio
    from .main import main
    asyncio.run(main())
except Exception as e:
    print(f"Fatal error: {e}", file=sys.stderr)
    traceback.print_exc()
    sys.exit(1)
