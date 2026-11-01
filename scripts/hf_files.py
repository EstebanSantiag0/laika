#!/usr/bin/env python3
"""List files of a Hugging Face model repo: path, size and LFS sha256.

Usage: hf_files.py <repo> [substring ...]
Prints tab-separated lines: path, size_bytes, sha256 (empty if not LFS).
Recurses into subdirectories. Only files whose path contains every
given substring are printed.
"""
import json
import sys
import urllib.request

API = "https://huggingface.co/api/models/{repo}/tree/main/{path}"


def tree(repo, path=""):
    with urllib.request.urlopen(API.format(repo=repo, path=path), timeout=60) as r:
        for item in json.load(r):
            if item["type"] == "directory":
                yield from tree(repo, item["path"])
            else:
                yield item


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    repo, needles = sys.argv[1], sys.argv[2:]
    for item in tree(repo):
        if all(n in item["path"] for n in needles):
            sha = (item.get("lfs") or {}).get("oid", "")
            print(f'{item["path"]}\t{item.get("size", 0)}\t{sha}')


if __name__ == "__main__":
    main()
