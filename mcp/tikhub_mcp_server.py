#!/usr/bin/env python3
"""MCP server that wraps TikHub REST API directly (bypass mcp.tikhub.io)."""
import json, sys, os, urllib.request, urllib.error, urllib.parse

API_BASE = "https://api.tikhub.io"
API_KEY = os.environ.get("TIKHUB_API_KEY", "")

TOOLS = [
    {
        "name": "tiktok_app_v3_fetch_video_search_result",
        "description": "Search TikTok videos by keyword",
        "inputSchema": {
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "Search keyword"},
                "count": {"type": "integer", "default": 5},
                "sort_type": {"type": "integer", "default": 1},
                "publish_time": {"type": "integer", "default": 30},
            },
            "required": ["keyword"],
        },
    },
    {
        "name": "tiktok_app_v3_fetch_one_video_v2",
        "description": "Get detailed metrics for a specific TikTok video",
        "inputSchema": {
            "type": "object",
            "properties": {
                "video_id": {"type": "string", "description": "TikTok video ID"},
            },
            "required": ["video_id"],
        },
    },
    {
        "name": "tiktok_app_v3_fetch_creator_search_insights_videos",
        "description": "Get trending videos from a creator",
        "inputSchema": {
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "Creator username or keyword"},
                "count": {"type": "integer", "default": 5},
            },
            "required": ["keyword"],
        },
    },
    {
        "name": "tiktok_web_fetch_post_detail",
        "description": "Get TikTok post detail via web API",
        "inputSchema": {
            "type": "object",
            "properties": {
                "video_id": {"type": "string", "description": "TikTok video ID"},
            },
            "required": ["video_id"],
        },
    },
    {
        "name": "instagram_v3_get_recommended_reels",
        "description": "Get recommended Instagram Reels",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "instagram_v2_fetch_tag_posts",
        "description": "Search Instagram posts by hashtag",
        "inputSchema": {
            "type": "object",
            "properties": {
                "tag": {"type": "string", "description": "Hashtag to search"},
                "count": {"type": "integer", "default": 5},
            },
            "required": ["tag"],
        },
    },
    {
        "name": "instagram_v2_fetch_user_posts",
        "description": "Get posts from an Instagram user",
        "inputSchema": {
            "type": "object",
            "properties": {
                "username": {"type": "string", "description": "Instagram username"},
                "count": {"type": "integer", "default": 5},
            },
            "required": ["username"],
        },
    },
    {
        "name": "youtube_web_search_video",
        "description": "Search YouTube videos",
        "inputSchema": {
            "type": "object",
            "properties": {
                "search_query": {"type": "string", "description": "Search query"},
                "order_by": {"type": "string", "default": "this_month"},
            },
            "required": ["search_query"],
        },
    },
]

TOOL_ROUTES = {
    "tiktok_app_v3_fetch_video_search_result": ("/api/v1/tiktok/app/v3/fetch_general_search_result", {"keyword": "keyword", "count": "count", "sort_type": "sort_type", "publish_time": "publish_time"}),
    "tiktok_app_v3_fetch_one_video_v2": ("/api/v1/tiktok/app/v3/fetch_one_video", {"video_id": "video_id"}),
    "tiktok_app_v3_fetch_creator_search_insights_videos": ("/api/v1/tiktok/app/v3/fetch_creator_search_insights_videos", {"keyword": "keyword", "count": "count"}),
    "tiktok_web_fetch_post_detail": ("/api/v1/tiktok/web/fetch_post_detail", {"video_id": "video_id"}),
    "instagram_v3_get_recommended_reels": ("/api/v1/instagram/v3/get_recommended_reels", {}),
    "instagram_v2_fetch_tag_posts": ("/api/v1/instagram/v2/fetch_tag_posts", {"tag": "tag", "count": "count"}),
    "instagram_v2_fetch_user_posts": ("/api/v1/instagram/v2/fetch_user_posts", {"username": "username", "count": "count"}),
    "youtube_web_search_video": ("/api/v1/youtube/web/search_video", {"search_query": "search_query", "order_by": "order_by"}),
}


def call_api(path: str, params: dict) -> dict:
    url = f"{API_BASE}{path}"
    filtered = {k: v for k, v in params.items() if v is not None}
    qs = urllib.parse.urlencode(filtered)
    full_url = f"{url}?{qs}" if qs else url
    req = urllib.request.Request(full_url, headers={
        "Authorization": f"Bearer {API_KEY}",
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:500]
        return {"error": f"HTTP {e.code}", "detail": body}
    except Exception as e:
        return {"error": str(e)}


KEY_WHITELIST = {
    "views", "play_count", "like_count", "comment_count", "share_count",
    "digg_count", "desc", "title", "url", "video_id", "nickname",
    "username", "engagement_rate", "id", "video", "status", "unique_id",
    "view_count", "subscriber_count", "follower_count", "name", "author", "caption",
}

def filter_json(data, max_items=3):
    if not data:
        return data
    if isinstance(data, dict) and data.get("code") == 200:
        inner = data.get("data", data)
    else:
        inner = data
    items_list = None
    if isinstance(inner, dict):
        raw_data = inner.get("data")
        if isinstance(raw_data, list) and len(raw_data) > 0:
            items_list = raw_data
    if items_list is None and isinstance(inner, list):
        items_list = inner
    if items_list is None and isinstance(data, list):
        items_list = data
    if items_list and isinstance(items_list, list):
        first = items_list[0]
        if isinstance(first, dict) and "aweme_info" in first:
            videos = []
            for item in items_list[:max_items]:
                info = item.get("aweme_info") or item
                v = {
                    "video_id": info.get("aweme_id") or info.get("id"),
                    "desc": info.get("desc"),
                    "author": extract_author(info),
                    "views": extract_stat(info, "play_count"),
                    "likes": extract_stat(info, "digg_count"),
                    "comments": extract_stat(info, "comment_count"),
                    "shares": extract_stat(info, "share_count"),
                }
                url = None
                if info.get("share_info"):
                    si = info["share_info"]
                    if isinstance(si, dict):
                        url = si.get("share_url")
                if not url and info.get("video"):
                    vobj = info["video"]
                    if isinstance(vobj, dict):
                        url = vobj.get("play_addr")
                v["url"] = url
                videos.append(v)
            return {"status": "success", "videos": videos}
        if isinstance(first, dict) and first.get("media_type") in (1, 2):
            posts = []
            for item in items_list[:max_items]:
                posts.append({
                    "id": item.get("id") or item.get("pk"),
                    "code": item.get("code"),
                    "username": item.get("user", {}).get("username") if isinstance(item.get("user"), dict) else None,
                    "likes": item.get("like_count"),
                    "comments": item.get("comment_count"),
                    "views": item.get("view_count") or item.get("play_count"),
                    "caption": item.get("caption", {}).get("text") if isinstance(item.get("caption"), dict) else None,
                    "url": f'https://www.instagram.com/p/{item.get("code")}/' if item.get("code") else None,
                })
            return {"status": "success", "posts": posts}
    if isinstance(data, dict):
        cleaned = clean_item(data)
        if cleaned:
            return cleaned
    return {"status": "success", "data": data}

def extract_author(info):
    author = info.get("author")
    if isinstance(author, dict):
        return author.get("unique_id") or author.get("nickname")
    return author

def extract_stat(info, key):
    stats = info.get("statistics")
    if isinstance(stats, dict):
        return stats.get(key)
    return None

def clean_item(obj):
    if not obj or not isinstance(obj, dict):
        return obj
    clean = {}
    for k, v in obj.items():
        lk = k.lower()
        if lk in KEY_WHITELIST:
            if lk in ("author", "video") and isinstance(v, dict):
                clean[k] = clean_item(v)
            else:
                clean[k] = v
    return clean


def handle_request(msg: dict) -> dict:
    msg_id = msg.get("id")
    method = msg.get("method")
    params = msg.get("params", {})

    if method == "initialize":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "tikhub-direct", "version": "1.0"},
        }}
    elif method == "tools/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS}}
    elif method == "tools/call":
        name = params.get("name", "")
        args = params.get("arguments", {})
        route = TOOL_ROUTES.get(name)
        if not route:
            return {"jsonrpc": "2.0", "id": msg_id, "result": {
                "content": [{"type": "text", "text": f"Unknown tool: {name}"}],
                "isError": True,
            }}
        path, arg_map = route
        api_params = {}
        for api_key, src_key in arg_map.items():
            val = args.get(src_key)
            if val is not None:
                api_params[api_key] = val
        result = call_api(path, api_params)
        filtered = filter_json(result)
        text = json.dumps(filtered, indent=2, ensure_ascii=False)
        if len(text) > 5000:
            text = text[:5000] + "\n... [TRUNCATED BY PROXY]"
        return {"jsonrpc": "2.0", "id": msg_id, "result": {
            "content": [{"type": "text", "text": text}],
        }}
    elif method == "notifications/initialized":
        return None
    else:
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
            resp = handle_request(msg)
            if resp is not None:
                sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
                sys.stdout.flush()
        except json.JSONDecodeError:
            sys.stderr.write(f"Invalid JSON: {line[:200]}\n")


if __name__ == "__main__":
    main()
