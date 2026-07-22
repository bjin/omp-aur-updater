import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "publish_arch_repo.py"
SPEC = importlib.util.spec_from_file_location("publish_arch_repo", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"Unable to load {MODULE_PATH}")
publisher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(publisher)


class FakeResponse:
    def __init__(self, status: int, body: bytes, headers: dict[str, str] | None = None):
        self.status = status
        self._body = body
        self._headers = headers or {}

    def read(self) -> bytes:
        return self._body

    def getheaders(self) -> list[tuple[str, str]]:
        return list(self._headers.items())


class FakeConnection:
    def __init__(self, response: FakeResponse):
        self.response = response
        self.sent = bytearray()
        self.closed = False

    def putrequest(self, *_: object) -> None:
        pass

    def putheader(self, *_: object) -> None:
        pass

    def endheaders(self) -> None:
        pass

    def send(self, data: bytes) -> None:
        self.sent.extend(data)

    def getresponse(self) -> FakeResponse:
        return self.response

    def close(self) -> None:
        self.closed = True


class UploadAssetRetryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.source = Path(self.temporary.name) / "package.pkg.tar.zst"
        self.source.write_bytes(b"package bytes")
        self.github = publisher.GitHubRelease("owner/repo", "token")

    def test_retries_gateway_timeout(self) -> None:
        connections = [
            FakeConnection(FakeResponse(504, b'{"message":"timed out"}')),
            FakeConnection(
                FakeResponse(201, b'{"id": 7, "name": "package.pkg.tar.zst"}')
            ),
        ]

        with patch.object(
            publisher.http.client, "HTTPSConnection", side_effect=connections
        ) as new_connection, patch.object(
            self.github, "_find_asset_with_size", return_value=None
        ), patch.object(publisher.time, "sleep") as sleep:
            asset = self.github.upload_asset(1, self.source, self.source.name)

        self.assertEqual(7, asset["id"])
        self.assertEqual(2, new_connection.call_count)
        self.assertEqual([call(1)], sleep.call_args_list)
        self.assertTrue(all(connection.closed for connection in connections))
        self.assertTrue(
            all(bytes(connection.sent) == self.source.read_bytes() for connection in connections)
        )

    def test_honors_retry_after_header(self) -> None:
        connections = [
            FakeConnection(
                FakeResponse(
                    429,
                    b'{"message":"rate limited"}',
                    {"Retry-After": "7"},
                )
            ),
            FakeConnection(
                FakeResponse(201, b'{"id": 8, "name": "package.pkg.tar.zst"}')
            ),
        ]

        with patch.object(
            publisher.http.client, "HTTPSConnection", side_effect=connections
        ), patch.object(self.github, "_find_asset_with_size", return_value=None), patch.object(
            publisher.time, "sleep"
        ) as sleep:
            asset = self.github.upload_asset(1, self.source, self.source.name)

        self.assertEqual(8, asset["id"])
        self.assertEqual([call(7)], sleep.call_args_list)

    def test_honors_rate_limit_reset_header(self) -> None:
        connections = [
            FakeConnection(
                FakeResponse(
                    403,
                    b'{"message":"API rate limit exceeded"}',
                    {
                        "X-RateLimit-Remaining": "0",
                        "X-RateLimit-Reset": "120",
                    },
                )
            ),
            FakeConnection(
                FakeResponse(201, b'{"id": 9, "name": "package.pkg.tar.zst"}')
            ),
        ]

        with patch.object(
            publisher.http.client, "HTTPSConnection", side_effect=connections
        ), patch.object(self.github, "_find_asset_with_size", return_value=None), patch.object(
            publisher.time, "sleep"
        ) as sleep, patch.object(publisher.time, "time", return_value=100):
            asset = self.github.upload_asset(1, self.source, self.source.name)

        self.assertEqual(9, asset["id"])
        self.assertEqual([call(25)], sleep.call_args_list)

    def test_uses_asset_created_before_gateway_timeout_response(self) -> None:
        existing_asset = {"id": 10, "name": self.source.name, "size": self.source.stat().st_size}
        connection = FakeConnection(FakeResponse(504, b'{"message":"timed out"}'))

        with patch.object(
            publisher.http.client, "HTTPSConnection", return_value=connection
        ) as new_connection, patch.object(
            self.github, "_find_asset_with_size", return_value=existing_asset
        ), patch.object(publisher.time, "sleep") as sleep:
            asset = self.github.upload_asset(1, self.source, self.source.name)

        self.assertIs(existing_asset, asset)
        self.assertEqual(1, new_connection.call_count)
        self.assertEqual([call(1)], sleep.call_args_list)


if __name__ == "__main__":
    unittest.main()
