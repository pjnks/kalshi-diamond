"""
bounded_dict.py
───────────────
OrderedDict subclass with max size and LRU eviction.

Used across DIAMOND modules to cap in-memory caches and prevent unbounded growth.
Compatible with Python 3.9+ (Mac) and 3.13+ (VM).
"""

from collections import OrderedDict


class BoundedDict(OrderedDict):
    """Dict with max size -- evicts oldest entries when full."""

    def __init__(self, max_size=5000, *args, **kwargs):
        self.max_size = max_size
        super().__init__(*args, **kwargs)

    def __setitem__(self, key, value):
        if key in self:
            self.move_to_end(key)
        super().__setitem__(key, value)
        while len(self) > self.max_size:
            self.popitem(last=False)
