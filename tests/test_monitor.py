"""Offline tests. Run:  python -m unittest discover -s tests -v

Network is never touched: fetchers are fed fake responses shaped like the documented API payloads.
These prove the filtering logic; they do NOT prove the live APIs still behave as documented. Use `python monitor.py --check` for that.
"""
import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import monitor as m  # noqa: E402

NOW = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)


class FakeResp:
    def __init__(self, data=None, status=200, content=b""):
        self._d, self.status_code, self.content, self.text = data, status, content, json.dumps(data)

    def json(self):
        return self._d

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def _hit(self, method, url, **kw):
        self.calls.append((method, url, kw.get("params")))
        for frag, resp in self.routes.items():
            if frag in url:
                return resp(kw) if callable(resp) else resp
        return FakeResp({}, 404)

    def get(self, url, **kw):
        return self._hit("GET", url, **kw)

    def post(self, url, **kw):
        return self._hit("POST", url, **kw)


def mkcfg(**kw):
    c = m.Cfg()
    c.dry_run = True
    c.tg_token = c.tg_chat = None
    c.validate = "off"
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def post(code_text, author="alice", age_min=10, src="bluesky", text=None):
    t = text if text is not None else f"free claude guest pass {code_text}"
    return m.Post(src, f"{src}-{author}-{code_text}", "https://example/post", author, t, t,
                  NOW - timedelta(minutes=age_min))


class Extraction(unittest.TestCase):
    def test_real_shapes(self):
        for url, code in [
            ("https://claude.ai/referral/wsKK8ipOeA?s=cowork&v=apps", "wsKK8ipOeA"),
            ("check claude.ai/referral/Me-pKK-zcA now", "Me-pKK-zcA"),
            ("(https://www.claude.ai/referral/OLPrl_WAyw)", "OLPrl_WAyw"),
            ("link: https://claude.ai/referral/qX2Lm5nF-w.", "qX2Lm5nF-w"),
        ]:
            self.assertEqual(m.extract_codes(url), [code], url)

    def test_lookalike_domains_rejected(self):
        for bad in [
            "https://claude.ai.evil.com/referral/wsKK8ipOeA",
            "https://claude-ai.com/referral/wsKK8ipOeA",
            "https://evilclaude.ai/referral/wsKK8ipOeA",
            "https://claude.ai@evil.com/referral/wsKK8ipOeA",
            "https://evil.com/claude.ai/referral/wsKK8ipOeA",
            "https://claude.ai/referral/wsKK8ipOeA.evil.com",
            "https://claude.ai/gift/redeem/wsKK8ipOeA",
        ]:
            self.assertEqual(m.extract_codes(bad), [], bad)

    def test_html_and_encoded(self):
        h = '<a href="https://claude.ai/referral/Ab3dEf9GhJ" rel="nofollow">https://claude.ai/referr...</a>'
        self.assertEqual(m.extract_codes(h), ["Ab3dEf9GhJ"])
        g = "https://www.google.com/url?q=https%3A%2F%2Fclaude.ai%2Freferral%2FAb3dEf9GhJ&sa=U"
        self.assertEqual(m.extract_codes(g), ["Ab3dEf9GhJ"])

    def test_dedupe_order(self):
        self.assertEqual(m.extract_codes("claude.ai/referral/Ab3dEf9GhJ claude.ai/referral/Zz9yXw8vUt claude.ai/referral/Ab3dEf9GhJ"),
                         ["Ab3dEf9GhJ", "Zz9yXw8vUt"])


class Plausibility(unittest.TestCase):
    def test_accepts_real_looking(self):
        for c in ["wsKK8ipOeA", "Me-pKK-zcA", "qX2Lm5nF-w", "OLPrl_WAyw", "TstThrd913"]:
            self.assertTrue(m.is_plausible(c), c)

    def test_rejects_placeholders(self):
        for c in ["YOUR-CODE-HERE", "xxxxxxxxxx", "yourcode12", "examplecode", "abcdefghij", "1234567890",
                  "ABCDEFGHIJ", "short", "aaaaaB1234", "your_link_Ab3", "PLACEHOLDER1"]:
            self.assertFalse(m.is_plausible(c), c)


class Pipeline(unittest.TestCase):
    def run_posts(self, posts, **cfgkw):
        cfg = mkcfg(**cfgkw)
        state = {"seen": {}, "authors": {}, "cursors": {}, "health": {}}
        sent = []
        orig = m.tg_send
        m.tg_send = lambda c, text, url=None: sent.append((text, url)) or True
        try:
            n = m.process_posts(posts, cfg, state, FakeSession({}), NOW)
        finally:
            m.tg_send = orig
        return n, sent, state

    def test_happy_path_and_canonical_link(self):
        p = post("https://claude.ai/referral/wsKK8ipOeA?utm=x")
        n, sent, st = self.run_posts([p])
        self.assertEqual(n, 1)
        self.assertEqual(sent[0][1], "https://claude.ai/referral/wsKK8ipOeA")  # tracking stripped, host fixed
        self.assertEqual(st["seen"]["wsKK8ipOeA"]["st"], "alerted")

    def test_dedupe_across_sources_and_runs(self):
        a = post("claude.ai/referral/wsKK8ipOeA", author="a", src="bluesky")
        b = post("claude.ai/referral/wsKK8ipOeA", author="b", src="reddit")
        n, _, st = self.run_posts([a, b])
        self.assertEqual(n, 1)
        cfg, sent = mkcfg(), []
        m.tg_send = lambda c, t, u=None: sent.append(t) or True
        self.assertEqual(m.process_posts([a], cfg, st, FakeSession({}), NOW), 0)

    def test_stale_and_future_dropped(self):
        old = post("claude.ai/referral/wsKK8ipOeA", age_min=60 * 50)
        fut = post("claude.ai/referral/Zz9yXw8vUt", age_min=-120)
        self.assertEqual(self.run_posts([old, fut])[0], 0)

    def test_placeholder_not_sent(self):
        self.assertEqual(self.run_posts([post("claude.ai/referral/YOUR-CODE-HERE")])[0], 0)

    def test_selling_posts_dropped(self):
        p = post("x", text="Selling claude pro guest pass $5 claude.ai/referral/wsKK8ipOeA dm me")
        self.assertEqual(self.run_posts([p])[0], 0)

    def test_bulk_poster_suppressed(self):
        codes = ["Ab3dEf9GhJ", "Zz9yXw8vUt", "Kk4mNn7PqR", "Ww2eRr5TtY"]
        posts = [post(f"claude.ai/referral/{c}", author="spammer") for c in codes]
        n, _, st = self.run_posts(posts)
        self.assertEqual(n, 0)
        self.assertTrue(all(st["seen"][c]["st"].startswith("suppressed") for c in codes))

    def test_three_codes_one_author_ok(self):
        codes = ["Ab3dEf9GhJ", "Zz9yXw8vUt", "Kk4mNn7PqR"]
        n, _, _ = self.run_posts([post(f"claude.ai/referral/{c}", author="generous") for c in codes])
        self.assertEqual(n, 3)

    def test_old_unrelated_link_only_post_dropped_by_score(self):
        p = post("x", text="claude.ai/referral/wsKK8ipOeA", age_min=60 * 30)
        p.text = "lol"  # no context words, 30h old => score 1 (<2)
        self.assertEqual(self.run_posts([p])[0], 0)

    def test_blocked_author(self):
        p = post("claude.ai/referral/wsKK8ipOeA", author="BadBot")
        self.assertEqual(self.run_posts([p], blocked_authors=["badbot"])[0], 0)

    def test_dead_link_dropped_in_drop_mode(self):
        cfg = mkcfg(validate="drop")
        state = {"seen": {}, "authors": {}, "cursors": {}, "health": {}}
        sess = FakeSession({"claude.ai/referral": FakeResp({}, 404)})
        m.tg_send = lambda *a, **k: True
        self.assertEqual(m.process_posts([post("claude.ai/referral/wsKK8ipOeA")], cfg, state, sess, NOW), 0)
        self.assertEqual(state["seen"]["wsKK8ipOeA"]["st"], "dropped:dead")

    def test_message_escapes_html(self):
        p = post("claude.ai/referral/wsKK8ipOeA", author="<b>x</b>")
        txt = m.build_message(p, "wsKK8ipOeA", NOW, "unknown", 5)
        self.assertNotIn("<b>x</b>", txt)
        self.assertIn("&lt;b&gt;", txt)


class Fetchers(unittest.TestCase):
    cfg = mkcfg(bsky_handle="h", bsky_password="p", x_bearer="t")

    def test_bluesky(self):
        sess = FakeSession({
            "createSession": FakeResp({"accessJwt": "jwt"}),
            "searchPosts": FakeResp({"posts": [{
                "uri": "at://did:plc:abc/app.bsky.feed.post/3kxyz", "author": {"handle": "a.bsky.social"},
                "record": {"text": "pass!", "createdAt": "2026-11-19T11:50:00Z",
                           "facets": [{"features": [{"$type": "x", "uri": "https://claude.ai/referral/wsKK8ipOeA"}]}]},
            }]}),
        })
        posts = m.fetch_bluesky(sess, mkcfg(bsky_handle="h", bsky_password="p"))
        self.assertEqual(m.extract_codes(posts[0].blob), ["wsKK8ipOeA"])
        self.assertEqual(posts[0].url, "https://bsky.app/profile/a.bsky.social/post/3kxyz")

    def test_reddit(self):
        listing = {"data": {"children": [
            {"kind": "t1", "data": {"body": "here https://claude.ai/referral/Ab3dEf9GhJ", "author": "u1", "name": "t1_a",
                                    "permalink": "/r/ClaudeAI/c/1", "created_utc": 1795089000}},
            {"kind": "t1", "data": {"body": "claude.ai/referral/Zz9yXw8vUt", "author": "AutoModerator", "name": "t1_b",
                                    "permalink": "/x", "created_utc": 1795089000}},
        ]}}
        posts = m.fetch_reddit(FakeSession({"reddit.com": FakeResp(listing)}), mkcfg())
        self.assertEqual({p.author for p in posts}, {"u1"})  # AutoModerator skipped

    def test_hn_href_beats_truncated_text(self):
        hit = {"objectID": "1", "author": "bob", "created_at_i": 1795089000,
               "comment_text": '<a href="https://claude.ai/referral/Ab3dEf9GhJ">https://claude.ai/referr...</a>'}
        posts = m.fetch_hn(FakeSession({"algolia": FakeResp({"hits": [hit]})}), mkcfg())
        self.assertEqual(m.extract_codes(posts[0].blob), ["Ab3dEf9GhJ"])

    def test_mastodon(self):
        st = [{"id": "9", "url": "https://mastodon.social/@a/9", "account": {"acct": "a"}, "created_at": "2026-11-19T11:00:00Z",
               "content": '<p><a href="https://claude.ai/referral/Ab3dEf9GhJ">claude.ai/referral/Ab3…</a></p>'}]
        posts = m.fetch_mastodon(FakeSession({"mastodon.social": FakeResp(st)}), mkcfg(mastodon_tags=["claudeai"]))
        self.assertEqual(m.extract_codes(posts[0].blob), ["Ab3dEf9GhJ"])

    def test_x_since_id_cursor_and_cap(self):
        j = {"data": [{"id": "200", "text": "t", "author_id": "7", "created_at": "2026-11-19T11:55:00Z",
                       "entities": {"urls": [{"expanded_url": "https://claude.ai/referral/Ab3dEf9GhJ"}]}}],
             "includes": {"users": [{"id": "7", "username": "carol"}]}, "meta": {"newest_id": "200"}}
        sess = FakeSession({"search/recent": FakeResp(j)})
        state = {"cursors": {"x": "150"}}
        posts = m.fetch_x_official(sess, self.cfg, state)
        self.assertEqual(sess.calls[0][2]["since_id"], "150")
        self.assertEqual(state["cursors"]["x"], "200")
        self.assertEqual(state["xreads"]["n"], 1)
        self.assertEqual((posts[0].author, m.extract_codes(posts[0].blob)), ("carol", ["Ab3dEf9GhJ"]))
        # cap reached => no request
        state["xreads"]["n"] = 100
        sess2 = FakeSession({})
        self.assertEqual(m.fetch_x_official(sess2, self.cfg, state), [])
        self.assertEqual(sess2.calls, [])

    def test_rss_google_alert_style(self):
        xml = (b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><link href="https://www.google.com/url?q=https://claude.ai/referral/Ab3dEf9GhJ&amp;sa=U"/>'
               b'<published>2026-11-19T11:00:00Z</published><content>guest pass</content></entry></feed>')
        sess = FakeSession({"feed": FakeResp({}, content=xml)})
        posts = m.fetch_rss(sess, mkcfg(extra_rss=["https://example.com/feed"]))
        self.assertEqual(m.extract_codes(posts[0].blob), ["Ab3dEf9GhJ"])


class Cycle(unittest.TestCase):
    def test_failing_source_alerts_once_and_state_is_stable(self):
        cfg = mkcfg()
        state = m.load_state("/nonexistent")
        sent = []
        orig = m.tg_send
        m.tg_send = lambda c, t, u=None: sent.append(t) or True
        try:
            sess = FakeSession({})  # everything 404 => every source fails
            for _ in range(10):
                m.cycle(cfg, state, sess, {}, NOW)
        finally:
            m.tg_send = orig
        self.assertEqual(len([s for s in sent if "keeps failing" in s]), 3)  # reddit, hn, mastodon: once each
        snap = json.dumps(state, sort_keys=True)
        m.cycle(cfg, state, FakeSession({}), {}, NOW)
        self.assertEqual(json.dumps(state, sort_keys=True), snap)  # capped counter => no endless commits

    def test_heartbeat_weekly(self):
        cfg, state, sent = mkcfg(), m.load_state("/nonexistent"), []
        orig = m.tg_send
        m.tg_send = lambda c, t, u=None: sent.append(t) or True
        try:
            m.heartbeat(cfg, state, NOW)                         # first run: records, no ping
            self.assertEqual(sent, [])
            m.heartbeat(cfg, state, NOW + timedelta(days=1))     # too soon
            self.assertEqual(sent, [])
            m.heartbeat(cfg, state, NOW + timedelta(days=8))     # due
            self.assertEqual(len(sent), 1)
        finally:
            m.tg_send = orig


if __name__ == "__main__":
    unittest.main()
