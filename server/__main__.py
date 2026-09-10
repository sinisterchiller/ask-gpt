"""Allow running the server as: python -m server"""
from server.main import main
import asyncio

asyncio.run(main())
