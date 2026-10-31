import asyncio
import os

import uvicorn

from kefu import configure_asyncio

configure_asyncio()

if __name__ == "__main__":
    config = uvicorn.Config(
        "kefu.app:app",
        host=os.getenv("BIND_HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "8000")),
        log_level="info",
    )
    asyncio.run(uvicorn.Server(config).serve())
