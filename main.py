"""Generic entry point: run the search agent on a research query.

Usage:
    python main.py                          # default sample query
    python main.py "your research question" # custom query
"""

import argparse
import asyncio
import sys

import search_agent


def main() -> None:
    # Reports contain Unicode symbols; Windows consoles default to GBK
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    parser = argparse.ArgumentParser(description="Deep research agent")
    parser.add_argument(
        "query",
        nargs="?",
        default="What is langgraph?",
        help="research question to investigate",
    )
    args = parser.parse_args()
    print(asyncio.run(search_agent.research(args.query)))


if __name__ == "__main__":
    main()
