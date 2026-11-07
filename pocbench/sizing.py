"""Upper-bound size estimates for candidate indexes.

These exist so Phase 0 can price index storage before the dataset does. They
ignore btree deduplication (Postgres 13+), which shrinks indexes whose leading
key repeats, so they overstate low-cardinality keys. Phase 1 replaces every
figure with a measured pg_relation_size.
"""

import math

PAGE_BYTES = 8192
PAGE_HEADER_BYTES = 24
BTREE_SPECIAL_BYTES = 16
INDEX_TUPLE_HEADER_BYTES = 8
ITEM_ID_BYTES = 4
LEAF_FILLFACTOR = 0.90
# Internal pages of a btree over this many rows are about 1% of leaf pages.
INTERNAL_OVERHEAD = 0.01
BRIN_PAGES_PER_RANGE = 128
# A BRIN minmax summary for one timestamptz column, with its line pointer.
BRIN_TUPLE_BYTES = 32


def maxalign(n: int) -> int:
    return (n + 7) // 8 * 8


def btree_bytes(rows: int, key_bytes: int, fillfactor: float = LEAF_FILLFACTOR) -> int:
    if rows <= 0:
        return PAGE_BYTES  # metapage only
    entry = INDEX_TUPLE_HEADER_BYTES + maxalign(key_bytes) + ITEM_ID_BYTES
    usable = (PAGE_BYTES - PAGE_HEADER_BYTES - BTREE_SPECIAL_BYTES) * fillfactor
    per_page = max(1, math.floor(usable / entry))
    leaf = math.ceil(rows / per_page)
    return (leaf + math.ceil(leaf * INTERNAL_OVERHEAD) + 1) * PAGE_BYTES


def brin_bytes(heap_bytes: int) -> int:
    heap_pages = math.ceil(heap_bytes / PAGE_BYTES)
    ranges = math.ceil(heap_pages / BRIN_PAGES_PER_RANGE)
    per_page = (PAGE_BYTES - PAGE_HEADER_BYTES) // BRIN_TUPLE_BYTES
    # metapage + revmap pages + summary pages
    revmap = math.ceil(ranges / ((PAGE_BYTES - PAGE_HEADER_BYTES) // 6))
    return (1 + revmap + math.ceil(ranges / per_page)) * PAGE_BYTES


def candidate_bytes(candidate: dict, dataset: dict) -> int:
    if "measured_bytes" in candidate:  # Phase 1 measured it on the corpus
        return candidate["measured_bytes"]
    table = candidate["table"]
    rows = dataset["rows"][table]
    if candidate.get("kind") == "brin":
        return brin_bytes(rows * dataset["row_bytes"][table])
    rows = math.ceil(rows * candidate.get("selectivity", 1.0))
    return btree_bytes(rows, candidate["key_bytes"])
