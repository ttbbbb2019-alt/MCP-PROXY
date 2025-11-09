"""
Legacy shim retained so existing tooling can still import the old path.
"""

from servers.minimal_server import main

__all__ = ["main"]

if __name__ == "__main__":
    main()
