#!/usr/bin/env bash
# Run as an unprivileged user after installing the PKGBUILD's dependencies and
# its _rust_min rustup toolchain. CARGO_HOME must be dedicated to this check.
set -euo pipefail

if [[ $# -ne 2 ]]; then
    echo "Usage: $0 AUR_DIRECTORY CARGO_TARGET_DIRECTORY" >&2
    exit 1
fi

aur_dir=$(realpath "$1")
mkdir -p "$2" "${CARGO_HOME:?Set a dedicated CARGO_HOME for this check}"
cache_target=$(realpath "$2")
cd "$aur_dir"
declare pkgname pkgver pkgrel _rust_min
declare -a _variants
# shellcheck source=/dev/null
source ./PKGBUILD

export RUSTUP_TOOLCHAIN="${_rust_min:?PKGBUILD must declare _rust_min}"
toolchain_bin=$(dirname "$(rustup which --toolchain "$_rust_min" rustc)")
export PATH="$toolchain_bin:$PATH"
export RUSTC="$toolchain_bin/rustc"
export RUSTDOC="$toolchain_bin/rustdoc"
export CARGO_INCREMENTAL=0

# Check the same stable override used by prepare(), not just rustup's default.
compiler=$(RUSTUP_TOOLCHAIN=stable rustc --version)
if [[ "$compiler" != "rustc $_rust_min "* ]]; then
    echo "Expected rustc $_rust_min, found: $compiler" >&2
    exit 1
fi
printf 'Checking %s %s-%s with %s\n' "$pkgname" "$pkgver" "$pkgrel" "$compiler"
RUSTUP_TOOLCHAIN=stable cargo --version

# Re-extraction resets the source patches on repeated runs. --nobuild executes
# the real prepare() but never build(), check(), or package().
makepkg --nobuild --noconfirm
export srcdir="$aur_dir/src"

# The cache action runs cargo metadata without _build_native's --config flag.
# Give it the same patched dependency graph, rather than letting it undo the
# system-opus lockfile changes during its post-build cache cleanup.
cp "$srcdir/system-opus.toml" "$CARGO_HOME/config.toml"

baseline_cpu=
for variant in "${_variants[@]}"; do
    if [[ "$variant" == baseline:* ]]; then
        baseline_cpu=${variant#baseline:}
    fi
done
if [[ -z "$baseline_cpu" ]]; then
    echo "PKGBUILD does not declare a baseline native variant." >&2
    exit 1
fi

cd "$srcdir/$pkgname-$pkgver"
mkdir -p target
ln -sfnT "$cache_target" target/baseline
_build_native baseline "$baseline_cpu"

addon=packages/natives/native/pi_natives.linux-x64-baseline.node
# Loading the built addon also proves that linking and the release stamp work.
bun -e '
    const addon = require(process.argv[1]);
    const expected = process.argv[2];
    const actual = addon.__piNativesBuildVersion();
    if (actual !== expected) {
        throw new Error("Native release stamp: expected " + expected + ", found " + actual);
    }
    console.log("baseline pi_natives: loaded, release " + actual);
' "./$addon" "$pkgver"
printf 'Minimum Rust verification passed: %s\n' "$addon"
