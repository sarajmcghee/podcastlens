"""Browser-level checks using fake HTTP responses, never live AWS credentials."""
import base64
import hashlib
import json
import os
from pathlib import Path
import unittest
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
SITE = "https://podcastlens.test/"
EPISODE = "a" * 64


class Dashboard(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True, executable_path=os.environ.get("CHROMIUM_EXECUTABLE"))

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.context = self.browser.new_context()
        self.page = self.context.new_page()
        self.requests = []
        self.unauthorized = False
        self.auth_url = None
        self.token_calls = []
        self.context.route("**/*", self.route)

    def tearDown(self):
        self.context.close()

    def route(self, route):
        request = route.request
        parsed = urlsplit(request.url)
        if parsed.hostname == "login.test":
            if parsed.path == "/oauth2/token":
                self.token_calls.append(parse_qs(request.post_data))
                route.fulfill(json={"access_token":"test-access-token"})
            else:
                self.auth_url = request.url
                route.fulfill(body="Sign in")
            return
        if parsed.hostname == "api.test":
            self.requests.append((request.method,parsed.path,request.post_data,request.headers))
            if self.unauthorized:
                route.fulfill(status=401,json={"error":"Unauthorized"}); return
            if parsed.path == "/episodes":
                route.fulfill(json={"episodes":[
                    {"episodeId":EPISODE,"title":"First recording <img src=x onerror=alert(1)>","status":"COMPLETED","source":"upload","updatedAt":"2026-11-08T14:10:04+00:00"},
                    {"episodeId":"b"*64,"title":"Still processing","status":"PROCESSING","source":"rss","updatedAt":"2026-11-08T14:11:04+00:00"}],"nextCursor":None})
            elif parsed.path == "/budget":
                route.fulfill(json={"usedSeconds":15,"capSeconds":600,"month":"2026-11"})
            elif parsed.path.endswith("/transcript"):
                route.fulfill(json={"matches":[{"context":"I saw a hummingbird.","offset":8}]} if parsed.query else
                              {"episode":{"title":"First recording"},"text":"I saw a hummingbird. <script>unsafe()</script>"})
            elif parsed.path == "/scan":
                route.fulfill(status=202,json={"message":"Feed check requested. Refresh recordings shortly."})
            else:
                route.fulfill(json={"feeds":[],"allowedHosts":[]})
            return
        if parsed.hostname == "podcastlens.test":
            if parsed.path == "/config.js":
                config={"apiUrl":"https://api.test","clientId":"browser-client","hostedUi":"https://login.test","redirectUri":SITE}
                route.fulfill(content_type="application/javascript",body="window.PODCASTLENS_CONFIG="+json.dumps(config)+";")
            else:
                name = "index.html" if parsed.path == "/" else parsed.path.lstrip("/")
                item = ROOT/"web"/name
                if item.is_file():
                    route.fulfill(path=str(item),content_type={".html":"text/html",".js":"application/javascript",".css":"text/css"}[item.suffix])
                else: route.fulfill(status=404,body="Missing")
            return
        route.abort()

    def open_signed_in(self):
        self.context.add_init_script("if(location.origin==='https://podcastlens.test')sessionStorage.setItem('pl.accessToken','test-access-token');")
        self.page.goto(SITE)
        expect(self.page.get_by_role("button",name="Read transcript")).to_be_visible()

    def test_signed_out_does_not_read_pipeline(self):
        self.page.goto(SITE)
        expect(self.page.get_by_role("button",name="Sign in to continue")).to_be_visible()
        self.assertEqual(self.requests,[])

    def test_existing_recordings_status_budget_and_safe_rendering(self):
        self.open_signed_in()
        expect(self.page.get_by_text("PROCESSING",exact=True)).to_be_visible()
        expect(self.page.get_by_text("COMPLETED",exact=True)).to_be_visible()
        expect(self.page.locator("#budget")).to_have_text("0m 15s / 10m 0s reserved this month")
        self.assertEqual(self.page.locator("#episodes img").count(),0)
        self.assertTrue(all(headers["authorization"]=="Bearer test-access-token" for *_,headers in self.requests))
        self.page.get_by_role("button",name="Read transcript").click()
        expect(self.page.locator("#transcript-text")).to_contain_text("<script>unsafe()</script>")
        self.assertEqual(self.page.locator("#transcript-text script").count(),0)
        self.page.get_by_label("Search this transcript").fill("hummingbird")
        self.page.get_by_role("button",name="Search",exact=True).click()
        expect(self.page.locator("#search-results")).to_contain_text("I saw a hummingbird.")

    def test_feed_changes_and_scan_use_pipeline_routes(self):
        self.open_signed_in()
        self.page.get_by_label("RSS feed URL").fill("https://feed.example/rss")
        self.page.get_by_label("Allowed audio and redirect hosts").fill("audio.example, cdn.example")
        self.page.get_by_role("button",name="Save feed").click()
        expect(self.page.locator("#toast")).to_contain_text("Feed saved")
        payload=next(body for method,path,body,_ in self.requests if method=="POST" and path=="/feeds")
        self.assertEqual(json.loads(payload),{"url":"https://feed.example/rss","allowedHosts":["audio.example","cdn.example"]})
        self.page.get_by_role("button",name="Check feeds now").click()
        expect(self.page.locator("#toast")).to_contain_text("Feed check requested")
        self.assertTrue(any(method=="POST" and path=="/scan" for method,path,*_ in self.requests))

    def test_expired_session_clears_recordings(self):
        self.open_signed_in(); self.unauthorized=True
        self.page.get_by_role("button",name="Refresh",exact=True).click()
        expect(self.page.locator("#app")).to_be_hidden()
        self.assertEqual(self.page.locator("#episodes").inner_text(),"")
        self.assertIsNone(self.page.evaluate("sessionStorage.getItem('pl.accessToken')"))

    def test_signin_pkce_state_and_access_token(self):
        self.page.goto(SITE)
        self.page.get_by_role("button",name="Sign in to continue").click()
        self.page.wait_for_url("https://login.test/**")
        query=parse_qs(urlsplit(self.auth_url).query)
        self.assertEqual(query["code_challenge_method"],["S256"])
        self.assertIn("podcastlens/access",query["scope"][0])
        self.page.goto(SITE+"?code=test-code&state="+query["state"][0])
        expect(self.page.get_by_role("button",name="Read transcript")).to_be_visible()
        verifier=self.token_calls[0]["code_verifier"][0]
        challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        self.assertEqual(challenge,query["code_challenge"][0])

    def test_bad_oauth_state_does_not_exchange_code(self):
        self.page.goto(SITE)
        self.page.get_by_role("button",name="Sign in to continue").click()
        self.page.wait_for_url("https://login.test/**")
        self.page.goto(SITE+"?code=test-code&state=wrong")
        expect(self.page.locator("#toast")).to_contain_text("Sign-in could not be verified")
        self.assertEqual(self.token_calls,[])
        self.assertEqual(self.requests,[])


if __name__ == "__main__":
    unittest.main(verbosity=2)
