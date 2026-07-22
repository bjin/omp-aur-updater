#!/usr/bin/env python3
"""Publish signed Arch Linux packages as GitHub Release assets."""

from __future__ import annotations

import argparse
import datetime as dt
import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

API_ROOT = "https://api.github.com"
API_VERSION = "2022-11-28"
PACKAGE_RE = re.compile(r"\.pkg\.tar(?:\.[A-Za-z0-9]+)+$")
MAX_UPLOAD_ATTEMPTS = 4
MAX_UPLOAD_BACKOFF_SECONDS = 60
TRANSIENT_UPLOAD_STATUSES = frozenset({500, 502, 503, 504})


class GitHubError(RuntimeError):
    def __init__(
        self, status: int, message: str, headers: dict[str, str] | None = None
    ) -> None:
        super().__init__(f"GitHub API returned HTTP {status}: {message}")
        self.status = status
        self.message = message
        self.headers = headers or {}


def is_rate_limited(error: GitHubError) -> bool:
    if error.status == 429:
        return True
    if error.status != 403:
        return False
    return (
        "retry-after" in error.headers
        or error.headers.get("x-ratelimit-remaining") == "0"
        or "rate limit" in error.message.lower()
    )


def is_retryable_upload_error(error: GitHubError) -> bool:
    return error.status in TRANSIENT_UPLOAD_STATUSES or is_rate_limited(error)


def upload_retry_delay(error: GitHubError, attempt: int) -> int:
    retry_after = error.headers.get("retry-after")
    if retry_after and retry_after.isdigit():
        return max(1, int(retry_after))

    if error.headers.get("x-ratelimit-remaining") == "0":
        reset = error.headers.get("x-ratelimit-reset")
        if reset and reset.isdigit():
            return max(1, int(reset) - int(time.time()) + 5)

    return min(MAX_UPLOAD_BACKOFF_SECONDS, 2**attempt)


class GitHubRelease:
    def __init__(self, repository: str, token: str) -> None:
        try:
            self.owner, self.name = repository.split("/", 1)
        except ValueError as error:
            raise ValueError("GitHub repository must have the form owner/name") from error
        if not self.owner or not self.name:
            raise ValueError("GitHub repository must have the form owner/name")
        self.repository = repository
        self.token = token

    def _headers(self, accept: str = "application/vnd.github+json") -> dict[str, str]:
        return {
            "Accept": accept,
            "Authorization": f"Bearer {self.token}",
            "User-Agent": "omp-aur-updater",
            "X-GitHub-Api-Version": API_VERSION,
        }

    def request(
        self,
        method: str,
        endpoint: str,
        payload: dict[str, Any] | None = None,
        accept: str = "application/vnd.github+json",
    ) -> Any:
        url = endpoint if endpoint.startswith("https://") else f"{API_ROOT}{endpoint}"
        data = None
        headers = self._headers(accept)
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                body = response.read()
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", errors="replace")
            try:
                message = json.loads(body).get("message", body)
            except json.JSONDecodeError:
                message = body
            raise GitHubError(error.code, str(message)) from error
        return json.loads(body) if body else None

    def ensure_tag(self, tag: str) -> None:
        encoded_tag = urllib.parse.quote(tag, safe="")
        endpoint = f"/repos/{self.repository}/git/ref/tags/{encoded_tag}"
        try:
            self.request("GET", endpoint)
            return
        except GitHubError as error:
            if error.status != 404:
                raise

        repository = self.request("GET", f"/repos/{self.repository}")
        encoded_branch = urllib.parse.quote(repository["default_branch"], safe="")
        branch = self.request(
            "GET",
            f"/repos/{self.repository}/git/ref/heads/{encoded_branch}",
        )
        print(f"Creating fixed Git tag {tag}")
        self.request(
            "POST",
            f"/repos/{self.repository}/git/refs",
            {"ref": f"refs/tags/{tag}", "sha": branch["object"]["sha"]},
        )

    def get_or_create_release(self, tag: str) -> dict[str, Any]:
        self.ensure_tag(tag)

        page = 1
        while True:
            releases = self.request(
                "GET",
                f"/repos/{self.repository}/releases?per_page=100&page={page}",
            )
            for release in releases:
                if release["tag_name"] != tag:
                    continue
                if release["draft"]:
                    print(f"Publishing existing draft release {tag}")
                    release = self.request(
                        "PATCH",
                        f"/repos/{self.repository}/releases/{release['id']}",
                        {"draft": False, "prerelease": False, "make_latest": "false"},
                    )
                if release["draft"]:
                    raise RuntimeError(f"GitHub left {tag} as a draft release")
                return release
            if len(releases) < 100:
                break
            page += 1

        print(f"Creating GitHub release {tag}")
        release = self.request(
            "POST",
            f"/repos/{self.repository}/releases",
            {
                "tag_name": tag,
                "name": tag,
                "body": "Signed Arch Linux repository managed by omp-aur-updater.",
                "draft": False,
                "prerelease": False,
                "make_latest": "false",
            },
        )
        if release["draft"]:
            raise RuntimeError(f"GitHub created {tag} as a draft release")
        return release

    def list_assets(self, release_id: int) -> list[dict[str, Any]]:
        assets: list[dict[str, Any]] = []
        page = 1
        while True:
            batch = self.request(
                "GET",
                f"/repos/{self.repository}/releases/{release_id}/assets?per_page=100&page={page}",
            )
            assets.extend(batch)
            if len(batch) < 100:
                return assets
            page += 1

    def _find_asset_with_size(
        self, release_id: int, name: str, expected_size: int
    ) -> dict[str, Any] | None:
        for asset in self.list_assets(release_id):
            if asset["name"] != name:
                continue
            if asset["size"] != expected_size:
                raise RuntimeError(
                    f"Release asset {name} exists with size {asset['size']}, "
                    f"expected {expected_size}"
                )
            return asset
        return None


    def download_asset(self, asset: dict[str, Any], destination: Path) -> None:
        request = urllib.request.Request(
            asset["url"],
            headers=self._headers("application/octet-stream"),
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                with destination.open("wb") as output:
                    shutil.copyfileobj(response, output, length=1024 * 1024)
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", errors="replace")
            raise GitHubError(error.code, body) from error

    def delete_asset(self, asset: dict[str, Any]) -> None:
        print(f"Deleting release asset {asset['name']}")
        self.request("DELETE", f"/repos/{self.repository}/releases/assets/{asset['id']}")

    def upload_asset(self, release_id: int, source: Path, name: str) -> dict[str, Any]:
        source_size = source.stat().st_size
        query = urllib.parse.urlencode({"name": name})
        target = (
            f"/repos/{self.owner}/{self.name}/releases/{release_id}/assets?{query}"
        )
        upload_may_have_succeeded = False

        def confirm_ambiguous_upload() -> dict[str, Any] | None:
            try:
                return self._find_asset_with_size(release_id, name, source_size)
            except GitHubError as error:
                print(
                    f"Could not verify release asset {name} after a transient upload "
                    f"failure: {error}",
                    file=sys.stderr,
                )
                return None

        for attempt in range(MAX_UPLOAD_ATTEMPTS):
            if upload_may_have_succeeded:
                uploaded = confirm_ambiguous_upload()
                if uploaded is not None:
                    print(f"Release asset {name} exists after a transient upload failure")
                    return uploaded

            connection = http.client.HTTPSConnection("uploads.github.com", timeout=600)
            try:
                connection.putrequest("POST", target)
                for header, value in self._headers().items():
                    connection.putheader(header, value)
                connection.putheader("Content-Type", "application/octet-stream")
                connection.putheader("Content-Length", str(source_size))
                connection.endheaders()
                with source.open("rb") as stream:
                    while chunk := stream.read(1024 * 1024):
                        connection.send(chunk)
                response = connection.getresponse()
                body = response.read()
                response_headers = {
                    header.lower(): value for header, value in response.getheaders()
                }
            finally:
                connection.close()

            if response.status == 201:
                print(f"Uploaded release asset {name}")
                return json.loads(body)

            message = body.decode("utf-8", errors="replace")
            try:
                message = json.loads(message).get("message", message)
            except json.JSONDecodeError:
                pass
            error = GitHubError(response.status, str(message), response_headers)
            if error.status == 422 and upload_may_have_succeeded:
                uploaded = confirm_ambiguous_upload()
                if uploaded is not None:
                    print(f"Release asset {name} exists after a transient upload failure")
                    return uploaded
            if not is_retryable_upload_error(error):
                raise error

            upload_may_have_succeeded = True
            if attempt == MAX_UPLOAD_ATTEMPTS - 1:
                uploaded = confirm_ambiguous_upload()
                if uploaded is not None:
                    print(f"Release asset {name} exists after a transient upload failure")
                    return uploaded
                raise error

            delay = upload_retry_delay(error, attempt)
            print(
                f"GitHub upload of {name} returned HTTP {error.status}; retrying in "
                f"{delay}s ({attempt + 2}/{MAX_UPLOAD_ATTEMPTS})",
                file=sys.stderr,
            )
            time.sleep(delay)

        raise AssertionError("unreachable")

    def replace_asset(
        self,
        release_id: int,
        assets_by_name: dict[str, dict[str, Any]],
        source: Path,
        name: str,
    ) -> dict[str, Any]:
        existing = assets_by_name.pop(name, None)
        if existing is not None:
            self.delete_asset(existing)
        uploaded = self.upload_asset(release_id, source, name)
        assets_by_name[name] = uploaded
        return uploaded


def run(command: list[str], *, env: dict[str, str] | None = None, stdin: str | None = None) -> str:
    result = subprocess.run(
        command,
        check=False,
        input=stdin,
        text=True,
        capture_output=True,
        env=env,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"Command failed ({' '.join(command)}): {detail}")
    return result.stdout


def import_signing_key(gpg_home: Path, private_key: str, expected_fingerprint: str) -> str:
    env = os.environ.copy()
    env["GNUPGHOME"] = str(gpg_home)
    run(["gpg", "--batch", "--import"], env=env, stdin=private_key)
    listing = run(["gpg", "--batch", "--with-colons", "--list-secret-keys"], env=env)
    fingerprints = {
        fields[9].upper()
        for line in listing.splitlines()
        if line.startswith("fpr:") and len(fields := line.split(":")) > 9
    }
    expected = expected_fingerprint.replace(" ", "").upper()
    if expected not in fingerprints:
        available = ", ".join(sorted(fingerprints)) or "none"
        raise RuntimeError(
            f"Signing secret does not contain expected fingerprint {expected}; found {available}"
        )
    return expected


def sign_file(path: Path, fingerprint: str, env: dict[str, str]) -> Path:
    signature = path.with_name(f"{path.name}.sig")
    run(
        [
            "gpg",
            "--batch",
            "--yes",
            "--pinentry-mode",
            "loopback",
            "--passphrase",
            "",
            "--local-user",
            fingerprint,
            "--output",
            str(signature),
            "--detach-sign",
            str(path),
        ],
        env=env,
    )
    return signature


def parse_timestamp(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def is_package_asset(name: str) -> bool:
    return not name.endswith(".sig") and PACKAGE_RE.search(name) is not None


def prune_old_packages(
    github: GitHubRelease,
    assets: list[dict[str, Any]],
    keep_newest: int,
    minimum_age: dt.timedelta,
    now: dt.datetime,
) -> None:
    packages = [asset for asset in assets if is_package_asset(asset["name"])]
    created = {asset["id"]: parse_timestamp(asset["created_at"]) for asset in packages}
    assets_by_name = {asset["name"]: asset for asset in assets}
    cutoff = now - minimum_age
    for package in packages:
        package_created = created[package["id"]]
        newer_count = sum(timestamp > package_created for timestamp in created.values())
        if newer_count < keep_newest or package_created > cutoff:
            continue
        github.delete_asset(package)
        signature = assets_by_name.get(f"{package['name']}.sig")
        if signature is not None:
            github.delete_asset(signature)


def build_database(
    database: Path,
    packages: list[Path],
    fingerprint: str,
    gpg_env: dict[str, str],
    verify_existing: bool,
) -> Path:
    command = ["repo-add", "--include-sigs", "--sign", "--key", fingerprint]
    if verify_existing:
        command.append("--verify")
    command.extend([str(database), *(str(package) for package in packages)])
    run(command, env=gpg_env)
    signature = database.with_name(f"{database.name}.sig")
    if not database.is_file() or not signature.is_file():
        raise RuntimeError("repo-add did not generate the signed repository database")
    return signature


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("packages", nargs="+", type=Path, help="built .pkg.tar.* files")
    parser.add_argument(
        "--github-repository",
        default=os.environ.get("GITHUB_REPOSITORY"),
        help="owner/name (defaults to GITHUB_REPOSITORY)",
    )
    parser.add_argument("--release", default="oh-my-pi", help="fixed GitHub release tag")
    parser.add_argument("--repo-name", default="oh-my-pi", help="pacman repository name")
    parser.add_argument("--key-fingerprint", required=True, help="expected signing key fingerprint")
    parser.add_argument(
        "--private-key-env",
        default="ARCH_REPO_GPG_PRIVATE_KEY",
        help="environment variable containing the armored private key",
    )
    parser.add_argument("--keep-newest", type=int, default=5)
    parser.add_argument("--minimum-age-days", type=int, default=7)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.github_repository:
        raise SystemExit("--github-repository or GITHUB_REPOSITORY is required")
    if args.keep_newest < 0 or args.minimum_age_days < 0:
        raise SystemExit("retention values must be non-negative")

    packages = [package.resolve() for package in args.packages]
    missing = [str(package) for package in packages if not package.is_file()]
    if missing:
        raise SystemExit(f"Package files do not exist: {', '.join(missing)}")
    invalid = [package.name for package in packages if not is_package_asset(package.name)]
    if invalid:
        raise SystemExit(f"Not Arch package filenames: {', '.join(invalid)}")
    if len({package.name for package in packages}) != len(packages):
        raise SystemExit("Package basenames must be unique")

    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        raise SystemExit("GH_TOKEN or GITHUB_TOKEN is required")
    private_key = os.environ.get(args.private_key_env)
    if not private_key:
        raise SystemExit(f"{args.private_key_env} is required")

    github = GitHubRelease(args.github_repository, token)
    release = github.get_or_create_release(args.release)
    release_id = int(release["id"])
    assets = github.list_assets(release_id)
    assets_by_name = {asset["name"]: asset for asset in assets}
    database_asset_name = f"{args.repo_name}.db"
    database_signature_asset_name = f"{database_asset_name}.sig"

    with tempfile.TemporaryDirectory(prefix="omp-arch-repo-") as temporary:
        workspace = Path(temporary)
        gpg_home = workspace / "gnupg"
        gpg_home.mkdir(mode=0o700)
        fingerprint = import_signing_key(gpg_home, private_key, args.key_fingerprint)
        gpg_env = os.environ.copy()
        gpg_env["GNUPGHOME"] = str(gpg_home)

        database = workspace / f"{args.repo_name}.db.tar.gz"
        database_signature = database.with_name(f"{database.name}.sig")
        existing_database = assets_by_name.get(database_asset_name)
        existing_database_signature = assets_by_name.get(database_signature_asset_name)
        if existing_database is not None:
            if existing_database_signature is None:
                raise RuntimeError(
                    f"Existing {database_asset_name} has no {database_signature_asset_name}"
                )
            print(f"Downloading existing repository database {database_asset_name}")
            github.download_asset(existing_database, database)
            github.download_asset(existing_database_signature, database_signature)
        elif existing_database_signature is not None:
            raise RuntimeError(
                f"Found {database_signature_asset_name} without {database_asset_name}"
            )

        package_signatures = [sign_file(package, fingerprint, gpg_env) for package in packages]
        database_signature = build_database(
            database,
            packages,
            fingerprint,
            gpg_env,
            verify_existing=existing_database is not None,
        )

        for package, signature in zip(packages, package_signatures, strict=True):
            github.replace_asset(release_id, assets_by_name, package, package.name)
            github.replace_asset(release_id, assets_by_name, signature, signature.name)

        current_assets = github.list_assets(release_id)
        prune_old_packages(
            github,
            current_assets,
            keep_newest=args.keep_newest,
            minimum_age=dt.timedelta(days=args.minimum_age_days),
            now=dt.datetime.now(dt.timezone.utc),
        )

        current_assets_by_name = {
            asset["name"]: asset for asset in github.list_assets(release_id)
        }
        github.replace_asset(
            release_id,
            current_assets_by_name,
            database,
            database_asset_name,
        )
        github.replace_asset(
            release_id,
            current_assets_by_name,
            database_signature,
            database_signature_asset_name,
        )

    print(
        "Repository published at "
        f"https://github.com/{args.github_repository}/releases/download/{args.release}"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (GitHubError, RuntimeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1) from error
