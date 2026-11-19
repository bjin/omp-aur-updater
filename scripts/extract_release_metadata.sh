#!/usr/bin/env bash
# Read the pin sources as data; do not unpack archive entries onto the runner.
set -euo pipefail
archive=$1
version=$2
destination=$3
root="oh-my-pi-$version"

tar -xzOf "$archive" "$root/Cargo.lock" > "$destination/Cargo.lock"
tar -xzOf "$archive" "$root/crates/ast/src/language/wasm_grammars.rs" \
    > "$destination/wasm_grammars.rs"
