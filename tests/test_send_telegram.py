import asyncio
from unittest import mock

import httpx

from helpers.send_telegram import send_telegram, _split_text


def test_split_text_short_single_chunk():
    assert _split_text("hello") == ["hello"]


def test_split_text_long_two_chunks():
    parts = _split_text("A" * 3000 + "|" + "B" * 3000)
    assert len(parts) == 2
    assert all(len(p) <= 4000 for p in parts)


def test_split_text_cuts_on_newline():
    text = "\n".join(f"plan {i} " + "x" * 80 for i in range(60))
    parts = _split_text(text)
    assert len(parts) > 1
    for p in parts:
        assert len(p) <= 4000


def test_split_text_cuts_on_space_when_no_newline():
    parts = _split_text("word " * 2000)
    assert len(parts) > 1
    for p in parts:
        assert len(p) <= 4000


def test_send_no_token_returns_none():
    async def go():
        from helpers import send_telegram as st
        old = st.TG_BOT_TOKEN
        st.TG_BOT_TOKEN = None
        try:
            return await send_telegram("hello")
        finally:
            st.TG_BOT_TOKEN = old
    assert asyncio.run(go()) is None


def test_send_short_single_post():
    async def go():
        from helpers import send_telegram as st
        posts = []

        class FakeClient:
            def __init__(self):
                self.post = asyncio.coroutine(self._post)

            async def _post(self, url, json=None, **kw):
                posts.append(json.get("text", ""))
                return httpx.Response(
                    200, request=httpx.Request("POST", url),
                    json={"ok": True, "result": {"message_id": 7}},
                )

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        old_tok, old_chat = st.TG_BOT_TOKEN, st.TG_CHAT_ID
        st.TG_BOT_TOKEN, st.TG_CHAT_ID = "TEST", "1"
        try:
            with mock.patch.object(st.httpx, "AsyncClient", return_value=FakeClient()):
                mid = await send_telegram("short message")
            assert mid == 7
            assert len(posts) == 1
            assert posts[0] == "short message"
        finally:
            st.TG_BOT_TOKEN, st.TG_CHAT_ID = old_tok, old_chat
    asyncio.run(go())


def test_send_long_all_chunks_and_buttons_on_first():
    async def go():
        from helpers import send_telegram as st
        posts = []

        class FakeClient:
            def __init__(self):
                self.post = asyncio.coroutine(self._post)

            async def _post(self, url, json=None, **kw):
                posts.append(json)
                return httpx.Response(
                    200, request=httpx.Request("POST", url),
                    json={"ok": True, "result": {"message_id": 42}},
                )

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        old_tok, old_chat = st.TG_BOT_TOKEN, st.TG_CHAT_ID
        st.TG_BOT_TOKEN, st.TG_CHAT_ID = "TEST", "1"
        buttons = [[{"text": "Go", "callback_data": "approve:1"}]]
        big = "A" * 3000 + "|" + "B" * 3000
        try:
            with mock.patch.object(st.httpx, "AsyncClient", return_value=FakeClient()):
                mid = await send_telegram(big, buttons)
            assert mid == 42  # msg_id du 1er chunk (porteur des boutons)
            assert len(posts) == 2, f"tous les chunks doivent partir, reçu {len(posts)}"
            assert "reply_markup" in posts[0]
            assert "reply_markup" not in posts[1]
            assert "A" * 100 in posts[0]["text"]
            assert "B" * 100 in posts[1]["text"]
        finally:
            st.TG_BOT_TOKEN, st.TG_CHAT_ID = old_tok, old_chat
    asyncio.run(go())


def test_send_long_preserves_all_plans():
    """Régression : un script de validation >4000 chars (7 plans) doit être envoyé
    COMPLET (tous les chunks), pas seulement le premier jusqu'au Plan 5."""
    script = "\n\n".join(f"Plan {i} (ts-ts)\nVideo: {'x'*700}\nVO: une phrase du plan {i}" for i in range(1, 8))
    assert len(script) > 4000

    async def go():
        from helpers import send_telegram as st
        posts = []

        class FakeClient:
            def __init__(self):
                self.post = asyncio.coroutine(self._post)

            async def _post(self, url, json=None, **kw):
                posts.append(json.get("text", ""))
                return httpx.Response(
                    200, request=httpx.Request("POST", url),
                    json={"ok": True, "result": {"message_id": 1}},
                )

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        old_tok, old_chat = st.TG_BOT_TOKEN, st.TG_CHAT_ID
        st.TG_BOT_TOKEN, st.TG_CHAT_ID = "TEST", "1"
        try:
            with mock.patch.object(st.httpx, "AsyncClient", return_value=FakeClient()):
                await send_telegram(script)
            full = "".join(posts)
            for i in range(1, 8):
                assert f"Plan {i}" in full, f"Plan {i} manquant — script envoyé incomplet"
        finally:
            st.TG_BOT_TOKEN, st.TG_CHAT_ID = old_tok, old_chat
    asyncio.run(go())