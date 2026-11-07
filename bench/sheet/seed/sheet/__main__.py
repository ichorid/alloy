"""`python3 -m sheet`: read commands from stdin, answer on stdout."""

import sys


def main() -> None:
    for line in sys.stdin:
        if line.strip():
            print("ERROR not implemented")


if __name__ == "__main__":
    main()
