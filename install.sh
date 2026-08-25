#!/bin/sh
set -e
cd "$(dirname "$0")"
mkdir -p "$HOME/.local/bin"
ln -sf "$(pwd)/bee" "$HOME/.local/bin/bee"
echo "bee installed -> ~/.local/bin/bee  (ensure ~/.local/bin is on your PATH)"
echo "next: bee setup --api   (cloud, 30 seconds)"
echo "  or: bee setup         (local model, auto-tiered to your RAM)"
