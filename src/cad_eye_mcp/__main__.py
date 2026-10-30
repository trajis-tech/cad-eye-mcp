import sys


def main() -> int:
    if "--check" in sys.argv:
        from .selfcheck import run

        return run()
    from .server import mcp

    mcp.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
