"""Kefu: durable, evidence-grounded after-sales assistance."""

__version__ = "0.1.0"


def configure_asyncio():
    """psycopg needs selector-based sockets on native Windows."""
    import asyncio
    import sys

    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
