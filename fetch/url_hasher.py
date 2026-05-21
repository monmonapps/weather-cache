"""HMAC-SHA256 based filename hashing.

cell_id (e.g. "396_1270") + SALT -> 16-char hex filename.
Must match the Java-side UrlHasher implementation byte-for-byte.
"""
from __future__ import annotations

import hashlib
import hmac


HASH_LENGTH = 16  # 64-bit hex; collision-free at ~1000 cells


def hmac_filename(salt: str, cell_id: str) -> str:
    digest = hmac.new(
        salt.encode("utf-8"),
        cell_id.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return digest[:HASH_LENGTH]


if __name__ == "__main__":
    import sys
    if len(sys.argv) != 3:
        print("Usage: python url_hasher.py <salt> <cell_id>", file=sys.stderr)
        sys.exit(1)
    print(hmac_filename(sys.argv[1], sys.argv[2]))
