#!/usr/bin/env bash
# Download the GGUF models listed in MANIFEST from Hugging Face into models/,
# resuming partial downloads and verifying each file's sha256 against the
# LFS hash reported by the Hugging Face API. Re-running skips verified files.
#
# Usage: scripts/download_models.sh [id ...]   (no ids = all entries)
set -euo pipefail

cd "$(dirname "$0")/.."

# id | repo | file | destination dir (relative to the project)
MANIFEST='
E|Qwen/Qwen3-Embedding-0.6B-GGUF|Qwen3-Embedding-0.6B-Q8_0.gguf|models/embed
A|unsloth/Qwen3.5-4B-GGUF|Qwen3.5-4B-Q4_K_M.gguf|models/llm
B|unsloth/gemma-4-E4B-it-GGUF|gemma-4-E4B-it-Q4_K_M.gguf|models/llm
C|unsloth/gemma-4-26B-A4B-it-GGUF|gemma-4-26B-A4B-it-UD-IQ3_XXS.gguf|models/llm
'

want() { [ $# -eq 0 ] && return 0; local id=$1; shift; for w in "$@"; do [ "$w" = "$id" ] && return 0; done; return 1; }

echo "$MANIFEST" | while IFS='|' read -r id repo file dest; do
  [ -z "$id" ] && continue
  want "$id" "$@" || continue
  mkdir -p "$dest"
  out="$dest/$file"
  expected=$(python3 -I scripts/hf_files.py "$repo" "$file" | awk -F'\t' -v f="$file" '$1==f {print $3}')
  if [ -z "$expected" ]; then echo "[$id] $file: not found in $repo" >&2; exit 1; fi
  if [ -f "$out" ] && [ -f "$out.sha256ok" ] && [ "$(cat "$out.sha256ok")" = "$expected" ]; then
    echo "[$id] $file: already verified"; continue
  fi
  echo "[$id] downloading $repo/$file"
  curl -fL -sS --retry 5 --retry-delay 10 -C - -o "$out" "https://huggingface.co/$repo/resolve/main/$file"
  actual=$(sha256sum "$out" | awk '{print $1}')
  if [ "$actual" != "$expected" ]; then echo "[$id] $file: sha256 mismatch ($actual != $expected)" >&2; exit 1; fi
  echo "$expected" > "$out.sha256ok"
  echo "[$id] $file: OK ($(du -h "$out" | cut -f1))"
done
