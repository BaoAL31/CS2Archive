"""Facebook Page + Instagram Graph helpers (system user token from .env).

List / inspect / delete published Page posts and Instagram media, and publish
new ones. Instagram and Page Reels accept a local file via Meta's resumable
rupload host (no public URL required).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

_scripts = Path(__file__).resolve().parents[1]
if str(_scripts) not in sys.path:
    sys.path.insert(0, str(_scripts))
import _pathsetup  # noqa: E402

_pathsetup.ensure()

from config import settings  # noqa: E402

GRAPH_VERSION = "v21.0"
GRAPH = f"https://graph.facebook.com/{GRAPH_VERSION}"
RUPLOAD = f"https://rupload.facebook.com"


class MetaGraphError(RuntimeError):
    def __init__(self, payload: dict[str, Any], *, status: int | None = None):
        err = payload.get("error") or {}
        msg = err.get("message") or json.dumps(payload)
        super().__init__(msg)
        self.payload = payload
        self.status = status
        self.code = err.get("code")
        self.subcode = err.get("error_subcode")


def _request(
    method: str,
    path: str,
    *,
    token: str,
    params: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
    timeout: int = 60,
) -> dict[str, Any]:
    params = dict(params or {})
    params["access_token"] = token
    url = f"{GRAPH}/{path.lstrip('/')}"
    body = None
    headers = {"Accept": "application/json"}
    if method.upper() == "GET":
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params, doseq=True)
    else:
        payload = dict(data or {})
        payload.update(params)
        body = urllib.parse.urlencode(payload).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=body, method=method.upper(), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
    except urllib.error.HTTPError as e:
        raw = e.read().decode() if e.fp else ""
        try:
            payload = json.loads(raw) if raw else {"error": {"message": str(e)}}
        except json.JSONDecodeError:
            payload = {"error": {"message": raw or str(e)}}
        raise MetaGraphError(payload, status=e.code) from e
    return json.loads(raw) if raw else {}


def _parse_http_error(e: urllib.error.HTTPError) -> dict[str, Any]:
    raw = e.read().decode() if e.fp else ""
    try:
        return json.loads(raw) if raw else {"error": {"message": str(e)}}
    except json.JSONDecodeError:
        return {"error": {"message": raw or str(e)}}


def _rupload_file(url: str, *, token: str, video: Path, timeout: int = 600) -> dict[str, Any]:
    size = video.stat().st_size
    req = urllib.request.Request(
        url,
        data=video.read_bytes(),
        method="POST",
        headers={
            "Authorization": f"OAuth {token}",
            "offset": "0",
            "file_size": str(size),
            "Content-Type": "application/octet-stream",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
    except urllib.error.HTTPError as e:
        raise MetaGraphError(_parse_http_error(e), status=e.code) from e
    return json.loads(raw) if raw else {}


class MetaGraph:
    def __init__(
        self,
        *,
        system_token: str | None = None,
        page_token: str | None = None,
        page_id: str | None = None,
        ig_user_id: str | None = None,
    ):
        self.system_token = system_token or settings.meta_system_user_token
        self.page_id = page_id or settings.meta_page_id
        self.ig_user_id = ig_user_id or settings.meta_ig_user_id
        self._page_token = page_token or settings.meta_page_access_token
        if not self.system_token:
            raise RuntimeError("META_SYSTEM_USER_TOKEN is not set in .env")

    @property
    def page_token(self) -> str:
        if self._page_token:
            return self._page_token
        if not self.page_id:
            raise RuntimeError("META_PAGE_ID is not set in .env")
        data = _request(
            "GET",
            f"{self.page_id}",
            token=self.system_token,
            params={"fields": "access_token"},
        )
        tok = data.get("access_token") or ""
        if not tok:
            raise RuntimeError("Could not derive a Page access token")
        self._page_token = tok
        return tok

    def get(self, path: str, *, token: str | None = None, **params: Any) -> dict[str, Any]:
        return _request("GET", path, token=token or self.system_token, params=params)

    def post(self, path: str, *, token: str | None = None, **data: Any) -> dict[str, Any]:
        return _request("POST", path, token=token or self.system_token, data=data)

    def delete(self, path: str, *, token: str | None = None) -> dict[str, Any]:
        return _request("DELETE", path, token=token or self.system_token)

    def page_posts(self, *, limit: int = 25) -> list[dict[str, Any]]:
        data = self.get(
            f"{self.page_id}/published_posts",
            token=self.page_token,
            fields="id,message,created_time,status_type,permalink_url,is_published",
            limit=limit,
        )
        return list(data.get("data") or [])

    def ig_profile(self) -> dict[str, Any]:
        return self.get(
            self.ig_user_id,
            fields="id,username,name,media_count,followers_count",
        )

    def ig_media(self, *, limit: int = 25) -> list[dict[str, Any]]:
        data = self.get(
            f"{self.ig_user_id}/media",
            fields="id,caption,media_type,media_product_type,timestamp,permalink,like_count,comments_count",
            limit=limit,
        )
        return list(data.get("data") or [])

    def delete_object(self, object_id: str, *, as_page: bool = False) -> dict[str, Any]:
        return self.delete(object_id, token=self.page_token if as_page else self.system_token)

    def publish_page_feed(self, message: str) -> dict[str, Any]:
        return self.post(f"{self.page_id}/feed", token=self.page_token, message=message)

    def publish_page_photo(self, *, url: str, caption: str = "") -> dict[str, Any]:
        payload: dict[str, Any] = {"url": url}
        if caption:
            payload["caption"] = caption
        return self.post(f"{self.page_id}/photos", token=self.page_token, **payload)

    def publish_ig_media(
        self,
        *,
        image_url: str | None = None,
        video_url: str | None = None,
        caption: str = "",
        media_type: str | None = None,
        poll_s: float = 5.0,
        timeout_s: float = 180.0,
    ) -> dict[str, Any]:
        """Create + publish an IG photo or Reel. URLs must be publicly reachable HTTPS."""
        create: dict[str, Any] = {}
        if caption:
            create["caption"] = caption
        if video_url:
            create["video_url"] = video_url
            create["media_type"] = media_type or "REELS"
            create["share_to_feed"] = "true"
        elif image_url:
            create["image_url"] = image_url
        else:
            raise ValueError("Provide image_url or video_url")
        container = self.post(f"{self.ig_user_id}/media", **create)
        creation_id = container.get("id")
        if not creation_id:
            raise MetaGraphError(container)
        return self._ig_wait_and_publish(creation_id, poll_s=poll_s, timeout_s=timeout_s)

    def _ig_wait_and_publish(
        self, creation_id: str, *, poll_s: float, timeout_s: float
    ) -> dict[str, Any]:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            status = self.get(creation_id, fields="status_code,status")
            code = (status.get("status_code") or "").upper()
            if code in {"FINISHED", "PUBLISHED"}:
                break
            if code in {"ERROR", "EXPIRED"}:
                raise MetaGraphError({"error": {"message": f"container {creation_id} {code}", **status}})
            time.sleep(poll_s)
        else:
            raise MetaGraphError({"error": {"message": f"container {creation_id} timed out"}})
        published = self.post(f"{self.ig_user_id}/media_publish", creation_id=creation_id)
        published["creation_id"] = creation_id
        return published

    def publish_ig_reel_file(
        self,
        video: Path,
        *,
        caption: str = "",
        poll_s: float = 5.0,
        timeout_s: float = 600.0,
    ) -> dict[str, Any]:
        """Upload a local mp4 as an Instagram Reel via resumable rupload."""
        video = Path(video)
        if not video.is_file():
            raise FileNotFoundError(video)
        create: dict[str, Any] = {
            "media_type": "REELS",
            "upload_type": "resumable",
            "share_to_feed": "true",
        }
        if caption:
            create["caption"] = caption
        container = self.post(f"{self.ig_user_id}/media", **create)
        creation_id = container.get("id")
        if not creation_id:
            raise MetaGraphError(container)
        uri = container.get("uri") or (
            f"{RUPLOAD}/ig-api-upload/{GRAPH_VERSION}/{creation_id}"
        )
        _rupload_file(uri, token=self.system_token, video=video)
        return self._ig_wait_and_publish(creation_id, poll_s=poll_s, timeout_s=timeout_s)

    def publish_page_reel_file(
        self,
        video: Path,
        *,
        description: str = "",
        title: str = "",
        scheduled_publish_time: int | None = None,
    ) -> dict[str, Any]:
        """Upload a local mp4 as a Facebook Page Reel.

        ``scheduled_publish_time`` is a Unix timestamp. Meta requires it to be
        at least ~10 minutes and at most ~29 days ahead; callers should fall
        back to immediate publish outside that window.
        """
        video = Path(video)
        if not video.is_file():
            raise FileNotFoundError(video)
        started = self.post(
            f"{self.page_id}/video_reels",
            token=self.page_token,
            upload_phase="start",
        )
        video_id = started.get("video_id")
        if not video_id:
            raise MetaGraphError(started)
        uri = started.get("upload_url") or (
            f"{RUPLOAD}/video-upload/{GRAPH_VERSION}/{video_id}"
        )
        _rupload_file(uri, token=self.page_token, video=video)
        finish: dict[str, Any] = {
            "upload_phase": "finish",
            "video_id": video_id,
            "video_state": "PUBLISHED",
        }
        if scheduled_publish_time:
            finish["video_state"] = "SCHEDULED"
            finish["scheduled_publish_time"] = int(scheduled_publish_time)
        if description:
            finish["description"] = description
        if title:
            finish["title"] = title
        done = self.post(f"{self.page_id}/video_reels", token=self.page_token, **finish)
        done["video_id"] = video_id
        return done


def _dump(obj: Any) -> None:
    print(json.dumps(obj, indent=2, default=str))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Facebook Page + Instagram Graph API")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("whoami", help="System user + Page + IG profile")

    lp = sub.add_parser("list-page", help="List published Facebook Page posts")
    lp.add_argument("--limit", type=int, default=25)

    li = sub.add_parser("list-ig", help="List Instagram posts/reels")
    li.add_argument("--limit", type=int, default=25)

    d = sub.add_parser("delete", help="Delete a Page post or IG media object by id")
    d.add_argument("object_id")
    d.add_argument("--page", action="store_true", help="Use the Page token (Page posts)")
    d.add_argument("--yes", action="store_true", required=True, help="Confirm deletion")

    pf = sub.add_parser("page-feed", help="Publish a text post to the Page")
    pf.add_argument("--message", required=True)
    pf.add_argument("--yes", action="store_true", required=True)

    pp = sub.add_parser("page-photo", help="Publish a photo to the Page from a public URL")
    pp.add_argument("--url", required=True)
    pp.add_argument("--caption", default="")
    pp.add_argument("--yes", action="store_true", required=True)

    ig = sub.add_parser("ig-publish", help="Publish an IG photo or Reel from a public HTTPS URL")
    ig.add_argument("--image-url")
    ig.add_argument("--video-url")
    ig.add_argument("--caption", default="")
    ig.add_argument("--media-type", default=None, help="Default REELS when --video-url is set")
    ig.add_argument("--yes", action="store_true", required=True)

    igf = sub.add_parser("ig-reel", help="Publish an IG Reel from a local mp4")
    igf.add_argument("--file", required=True)
    igf.add_argument("--caption", default="")
    igf.add_argument("--yes", action="store_true", required=True)

    pr = sub.add_parser("page-reel", help="Publish a Facebook Page Reel from a local mp4")
    pr.add_argument("--file", required=True)
    pr.add_argument("--description", default="")
    pr.add_argument("--title", default="")
    pr.add_argument("--schedule-at", help="Unix timestamp or ISO-8601 UTC; Graph native schedule")
    pr.add_argument("--yes", action="store_true", required=True)

    args = p.parse_args(argv)
    api = MetaGraph()

    if args.cmd == "whoami":
        _dump({
            "me": api.get("me", fields="id,name"),
            "page": api.get(api.page_id, fields="id,name,instagram_business_account"),
            "instagram": api.ig_profile(),
        })
        return 0
    if args.cmd == "list-page":
        _dump(api.page_posts(limit=args.limit))
        return 0
    if args.cmd == "list-ig":
        _dump(api.ig_media(limit=args.limit))
        return 0
    if args.cmd == "delete":
        _dump(api.delete_object(args.object_id, as_page=args.page))
        return 0
    if args.cmd == "page-feed":
        _dump(api.publish_page_feed(args.message))
        return 0
    if args.cmd == "page-photo":
        _dump(api.publish_page_photo(url=args.url, caption=args.caption))
        return 0
    if args.cmd == "ig-publish":
        _dump(api.publish_ig_media(
            image_url=args.image_url,
            video_url=args.video_url,
            caption=args.caption,
            media_type=args.media_type,
        ))
        return 0
    if args.cmd == "ig-reel":
        _dump(api.publish_ig_reel_file(Path(args.file), caption=args.caption))
        return 0
    if args.cmd == "page-reel":
        sched = None
        if args.schedule_at:
            raw = args.schedule_at.strip()
            if raw.isdigit():
                sched = int(raw)
            else:
                dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                sched = int(dt.timestamp())
        _dump(api.publish_page_reel_file(
            Path(args.file),
            description=args.description,
            title=args.title,
            scheduled_publish_time=sched,
        ))
        return 0
    p.error(f"unknown command {args.cmd}")
    return 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except MetaGraphError as e:
        print(f"[META_GRAPH_ERROR] {e}", file=sys.stderr)
        raise SystemExit(1)
