"""Read-only Zotero Local API adapter."""

from __future__ import annotations

import json
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

from .files import PipelineError


def validate_url(value: str) -> str:
    if not isinstance(value, str):
        raise PipelineError("zotero_url must be a string")
    parts = urlsplit(value.strip())
    if parts.scheme != "http" or parts.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise PipelineError("Zotero API must use a local HTTP loopback address")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise PipelineError("Zotero API URL must not contain credentials, a query string, or a fragment")
    return value.strip()


def key(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Z0-9]{8}", value):
        raise PipelineError(f"Zotero key must be 8 uppercase letters or digits: {value}")
    return value


class Zotero:
    def __init__(self, url: str = "http://localhost:23119/api/"):
        self.url = validate_url(url).rstrip("/") + "/"
        self.server_id = ""
        self.last_version = ""
        self.opener = build_opener(ProxyHandler({}))

    def new_session(self) -> Zotero:
        """Return an independent client pinned to the current Zotero database."""
        session = Zotero(self.url)
        session.server_id = self.server_id
        return session

    def _read(self, path: str) -> bytes:
        headers = {"Zotero-API-Version": "3"}
        if self.server_id:
            headers["Zotero-Server-Id"] = self.server_id
        request = Request(self.url + path, headers=headers)
        try:
            with self.opener.open(request, timeout=10) as response:
                server_id = response.headers.get("Zotero-Server-Id", "")
                if self.server_id and server_id and server_id != self.server_id:
                    raise PipelineError("Zotero local database identity changed during the request")
                if server_id:
                    self.server_id = server_id
                self.last_version = response.headers.get("Last-Modified-Version", "")
                body = response.read()
        except HTTPError as error:
            hints = {
                403: "allow other applications on this machine to access Zotero in Zotero settings",
                404: "check the Collection key, or confirm the attachment still exists and is downloaded",
            }
            raise PipelineError(f"Zotero API returned {error.code}: {hints.get(error.code, error.reason)}") from error
        except (OSError, UnicodeError, URLError) as error:
            raise PipelineError("cannot connect to the Zotero Local API; confirm Zotero is running") from error
        return body

    def request(self, path: str) -> object:
        try:
            return json.loads(self._read(path).decode("utf-8"))
        except ValueError as error:
            raise PipelineError("Zotero API response is not valid JSON") from error
        except UnicodeError as error:
            raise PipelineError("Zotero API JSON is not UTF-8") from error

    def request_text(self, path: str) -> str:
        try:
            return self._read(path).decode("utf-8").strip()
        except UnicodeError as error:
            raise PipelineError("Zotero API text is not UTF-8") from error

    def listing(self, path: str) -> list[dict]:
        result: list[dict] = []
        versions: set[str] = set()
        start = 0
        while True:
            separator = "&" if "?" in path else "?"
            page = self.request(f"{path}{separator}limit=100&start={start}")
            if not isinstance(page, list):
                raise PipelineError("Zotero API pagination response is not a list")
            if self.last_version:
                versions.add(self.last_version)
            if len(versions) > 1:
                raise PipelineError("Zotero data changed during pagination; run the command again")
            result.extend(page)
            if len(page) < 100:
                return result
            start += len(page)

    def collections(self) -> list[dict]:
        return self.listing("users/0/collections")

    def items(self, collection: str) -> list[dict]:
        return self.listing(f"users/0/collections/{key(collection)}/items/top")

    def children(self, item_key: str) -> list[dict]:
        return self.listing(f"users/0/items/{key(item_key)}/children")

    def attachment_file_url(self, attachment_key: str) -> str:
        return self.request_text(f"users/0/items/{key(attachment_key)}/file/view/url")
