"""Short random ids for cells and sheets in saved files."""

import random


def new_id() -> str:
    """A short id, unique enough for one workbook."""
    return f"c{random.randint(0, 999):03d}"
