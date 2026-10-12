# Development

## Daily minimum-Rust check

[`check-oh-my-pi-rust-min.yml`](.github/workflows/check-oh-my-pi-rust-min.yml) runs daily at **05:43 UTC** and can also be started with **Run workflow** in GitHub Actions:

```sh
gh workflow run check-oh-my-pi-rust-min.yml
```

It clones the current `oh-my-pi` AUR package, reads `_rust_min` from its PKGBUILD, and installs that exact rustup toolchain with the minimal profile. It checks the source checksums, runs the package's `prepare()`, and calls `_build_native` for **baseline only**, retaining the PKGBUILD's CPU setting, Cargo features, build profile, system-library patches, and `RUSTC_BOOTSTRAP=1`. It then loads the resulting `.node` addon with Bun and verifies its release stamp. It does not build the modern variant or full Arch package, update `_rust_min`, or publish anything.

The build uses the pinned toolchain's actual binaries and an explicit `RUSTC`, so the PKGBUILD's `RUSTUP_TOOLCHAIN=stable` assignments cannot select a newer compiler. Compilation or addon-loading failures fail the workflow.

### Cache policy

The workflow uses [`Swatinem/rust-cache`](https://github.com/Swatinem/rust-cache) to retain Cargo downloads and compiled **dependency** artifacts for the baseline target. Incremental compilation is disabled. Workspace-crate outputs, the modern target, final addons, and the rustup installation are not cached; the minimal toolchain is installed on each run. The action prunes unused dependencies and redundant extracted registry sources, retaining the exceptions needed by native `-sys` crates.

- The cache prefix contains `_rust_min`: raising it prevents both exact and fallback restores from an older compiler's cache.
- Within that namespace, the action keys caches by compiler/environment and Cargo manifests/lockfiles, with dependency-cache fallback across lockfile changes. Native package versions, preparation/build functions, packaging patches, and the build helper also contribute to the environment key.
- The target-cache path is independent of `pkgver`. There is no date or run ID in the key, avoiding a new full snapshot just because another daily check ran.
- Caches are saved only after success. Every run still invokes the pinned compiler build; a cache hit does not skip verification.

[GitHub's default cache allowance is 10 GB per repository](https://docs.github.com/en/actions/reference/workflows-and-actions/dependency-caching#usage-limits-and-eviction-policy). Entries unused for more than seven days are removed; daily restores keep the active cache in use. **Seven-day retention is not guaranteed:** storage pressure can evict entries sooner. Old compiler namespaces are left for GitHub to evict rather than deleting other workflows' caches or increasing paid storage.

