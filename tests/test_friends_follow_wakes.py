"""Follow wakes are off unless the connector opts in.

With --wake-on-follows, becoming mutual friends wakes once, whichever side followed back. A one-way
follow never wakes. The listing omits follows unless the poll asks, so a connector that has not opted in
is unchanged. An older gateway that rejects the parameter still wakes jobs.
"""
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from support.public_harness import C, FriendsGateway, cli

import bot_client as P


def bot_id(char):
    return "bot_" + char * 16


class FollowWakeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="yobiru follow wakes ")
        cls.gw = FriendsGateway(Path(cls._tmp.name)).start()
        cls.n = 0

    @classmethod
    def tearDownClass(cls):
        cls.gw.stop()
        cls._tmp.cleanup()

    def handle(self, prefix):
        self.__class__.n += 1
        return "%s%d" % (prefix, self.__class__.n)

    def token_for(self, home, bot):
        creds = json.loads((Path(home) / "public" / "credentials.json").read_text())
        for entry in creds["gateways"].values():
            pool = entry.get("connectors") or {}
            if bot in pool:
                return pool[bot]["token"]
        self.fail("no connector credential for %s" % bot)

    def listing(self, token, follows=None):
        path = "/v1/connector/jobs" + (("?follows=%s" % follows) if follows else "")
        return self.gw.ok("GET", path, None, token)

    def poll(self, token, cursor, wait, follows=None):
        query = "wait=%d&cursor=%s" % (wait, cursor)
        if follows:
            query += "&follows=%s" % follows
        started = time.monotonic()
        status, obj = C.api_call(self.gw.url, "GET", "/v1/connector/jobs?" + query, None, token,
                                 timeout=wait + 10)
        return status, obj, time.monotonic() - started

    def configure(self, home, bot, log, follows=False):
        script = "import sys; open(sys.argv[1], 'a').write(' '.join(sys.argv[2:]) + '\\n')"
        flags = ["--wake-on-follows"] if follows else []
        args = ["connector", "config", bot] + flags + [
            "--exec", sys.executable, "-c", script, str(log), "{kind}", "{phase}", "{follower_bot_id}"]
        code, out, err = cli(home, *args)
        self.assertEqual(0, code, (args, err, out))

    def connector_for(self, prefix, follows=False):
        handle = self.handle(prefix)
        bot, source = self.gw.owned_bot(handle)
        _, wid = self.gw.native(bot)
        home = Path(self._tmp.name) / ("home-%s" % handle)
        log = Path(self._tmp.name) / ("wakes-%s.log" % handle)
        token_file = Path(self._tmp.name) / ("conn-%s.token" % handle)
        code, out, err = cli(home, "owner", "login", self.gw.url, "--token-file",
                             str(self.gw.home / "owner.token"))
        self.assertEqual(0, code, err)
        code, out, err = cli(home, "connector", "bind", bot, wid, "--token-file", str(token_file))
        self.assertEqual(0, code, err)
        code, out, err = cli(home, "connector", "login", self.gw.url, "--token-file", str(token_file))
        self.assertEqual(0, code, err)
        self.configure(home, bot, log, follows)
        return bot, source, handle, home, log, self.token_for(home, bot)

    def run_once(self, home, bot):
        code, out, err = cli(home, "connector", "run", bot, "--once")
        self.assertEqual(0, code, err)
        return out

    def lines(self, log):
        return log.read_text().splitlines() if log.is_file() else []

    def assert_no_name(self, obj, *names):
        blob = json.dumps(obj)
        for name in names:
            self.assertNotIn(name, blob)
        self.assertNotIn("Synthetic", blob, "no display name")

    def test_with_the_switch_off_a_friendship_changes_nothing(self):
        bot, source, _, home, log, token = self.connector_for("off")
        other, other_source = self.gw.owned_bot(self.handle("pal"))
        self.assertEqual(201, self.gw.follow(source, other)[0])
        self.assertEqual(201, self.gw.follow(other_source, bot)[0])
        plain = self.listing(token)
        self.assertNotIn("follows", plain, "a connector that did not ask gets today's listing")
        stranger, stranger_source = self.gw.owned_bot(self.handle("fan"))
        followed = []
        timer = threading.Timer(0.3, lambda: followed.append(self.gw.follow(stranger_source, bot)))
        timer.start()
        self.addCleanup(timer.cancel)
        status, during, took = self.poll(token, plain["cursor"], 1)
        timer.join(5)
        self.assertEqual(200, status, during)
        self.assertEqual(201, followed[0][0], followed)
        self.assertGreaterEqual(took, 0.9, "a follow does not end the wait when the switch is off")
        self.assertNotIn("follows", during)
        self.assertEqual(plain["cursor"], during["cursor"])
        self.assertEqual([], self.run_once(home, bot)["woken"])
        self.assertEqual([], self.lines(log))

    def test_a_follow_back_wakes_once_and_following_someone_does_not(self):
        bot, source, _, home, log, token = self.connector_for("back", follows=True)
        other, other_source = self.gw.owned_bot(self.handle("guoyu"))
        before = self.listing(token, "mutual")
        self.assertEqual([], before["follows"])
        followed = []
        timer = threading.Timer(0.3, lambda: followed.append(self.gw.follow(source, other)))
        timer.start()
        self.addCleanup(timer.cancel)
        status, during, took = self.poll(token, before["cursor"], 1, "mutual")
        timer.join(5)
        self.assertEqual(200, status, during)
        self.assertEqual(201, followed[0][0], followed)
        self.assertGreaterEqual(took, 0.9, "following someone who does not follow back does not end the wait")
        self.assertEqual([], during["follows"])
        self.assertEqual([], self.run_once(home, bot)["woken"])
        self.assertEqual([], self.lines(log))

        waiting = self.listing(token, "mutual")
        arrived = []
        timer = threading.Timer(0.4, lambda: arrived.append(self.gw.follow(other_source, bot)))
        timer.start()
        self.addCleanup(timer.cancel)
        status, woken, took = self.poll(token, waiting["cursor"], 8, "mutual")
        timer.join(5)
        self.assertEqual((200, 201), (status, arrived[0][0]))
        self.assertLess(took, 3, "their follow-back ends the held poll")
        self.assertEqual([{"bot_id": other, "revision": 1, "kind": "friends"}], woken["follows"])
        self.assert_no_name(woken, "guoyu")
        plain = self.listing(token)
        self.assertNotIn("follows", plain, "an older connector still sees no follows")
        status, quiet, took = self.poll(token, plain["cursor"], 1)
        self.assertGreaterEqual(took, 0.9)
        self.assertEqual(plain["cursor"], quiet["cursor"])
        first = self.run_once(home, bot)
        self.assertEqual([{"ref": other, "kind": "follow", "phase": "friends", "outcome": "accepted"}],
                         first["woken"])
        self.assertEqual([], first["recovered"])
        self.assert_no_name(first, "guoyu")
        second = self.run_once(home, bot)
        self.assertEqual([], second["woken"], "a restart does not wake again")
        self.assertEqual([], second["recovered"])
        self.assertEqual(["yobiru.follow friends %s" % other], self.lines(log))
        self.assertEqual([], self.listing(token, "mutual")["follows"])
        again = self.gw.follow(other_source, bot)
        self.assertEqual((200, True), (again[0], again[1]["duplicate"]))
        self.assertEqual([], self.run_once(home, bot)["woken"])
        self.assertEqual(1, len(self.lines(log)))

    def test_following_them_back_wakes_once(self):
        bot, source, _, home, log, token = self.connector_for("rev", follows=True)
        other, other_source = self.gw.owned_bot(self.handle("nyx"))
        self.assertEqual(201, self.gw.follow(other_source, bot)[0])
        self.assertEqual([], self.listing(token, "mutual")["follows"], "a one-way follower is not a friendship")
        self.assertEqual([], self.run_once(home, bot)["woken"])
        self.assertEqual(201, self.gw.follow(source, other)[0])
        listed = self.listing(token, "mutual")
        self.assertEqual([{"bot_id": other, "revision": 1, "kind": "friends"}], listed["follows"])
        self.assert_no_name(listed, "nyx")
        self.assertEqual([other], [item["ref"] for item in self.run_once(home, bot)["woken"]])
        self.assertEqual([], self.run_once(home, bot)["woken"])
        self.assertEqual(["yobiru.follow friends %s" % other], self.lines(log))

    def test_two_new_friendships_wake_twice(self):
        bot, source, _, home, log, token = self.connector_for("pair", follows=True)
        friends = []
        for _ in range(2):
            other, other_source = self.gw.owned_bot(self.handle("duo"))
            self.assertEqual(201, self.gw.follow(source, other)[0])
            self.assertEqual(201, self.gw.follow(other_source, bot)[0])
            friends.append(other)
        listed = [item["bot_id"] for item in self.listing(token, "mutual")["follows"]]
        self.assertEqual(sorted(friends), sorted(listed))
        woken = [item["ref"] for item in self.run_once(home, bot)["woken"]]
        self.assertEqual(sorted(friends), sorted(woken))
        self.assertEqual([], self.run_once(home, bot)["woken"])
        self.assertEqual(sorted("yobiru.follow friends %s" % friend for friend in friends),
                         sorted(self.lines(log)))

    def test_one_way_follows_never_wake(self):
        bot, _, _, home, log, token = self.connector_for("burst", follows=True)
        before = self.listing(token, "mutual")
        for _ in range(5):
            _, follower_source = self.gw.owned_bot(self.handle("spam"))
            self.assertEqual(201, self.gw.follow(follower_source, bot)[0])
        self.assertEqual([], self.listing(token, "mutual")["follows"])
        self.assertEqual(before["cursor"], self.listing(token, "mutual")["cursor"])
        self.assertEqual([], self.run_once(home, bot)["woken"])
        self.assertEqual([], self.lines(log))

    def test_unfollow_does_not_wake_and_following_again_wakes_once(self):
        bot, source, _, home, log, token = self.connector_for("again", follows=True)
        other, other_source = self.gw.owned_bot(self.handle("fan"))
        self.assertEqual(201, self.gw.follow(source, other)[0])
        self.assertEqual(201, self.gw.follow(other_source, bot)[0])
        self.run_once(home, bot)
        self.assertEqual(200, self.gw.unfollow(other_source, bot)[0])
        self.assertEqual([], self.listing(token, "mutual")["follows"])
        self.assertEqual([], self.run_once(home, bot)["woken"])
        self.assertEqual(1, len(self.lines(log)))
        self.assertEqual(201, self.gw.follow(other_source, bot)[0])
        self.assertEqual([{"bot_id": other, "revision": 3, "kind": "friends"}],
                         self.listing(token, "mutual")["follows"])
        self.run_once(home, bot)
        self.assertEqual([], self.run_once(home, bot)["woken"])
        self.assertEqual(["yobiru.follow friends %s" % other] * 2, self.lines(log))

    def test_a_restart_during_the_wake_is_uncertain_and_is_not_repeated(self):
        bot, source, _, home, log, token = self.connector_for("crash", follows=True)
        other, other_source = self.gw.owned_bot(self.handle("lost"))
        self.assertEqual(201, self.gw.follow(source, other)[0])
        self.assertEqual(201, self.gw.follow(other_source, bot)[0])
        wake_id = "lost-follow-01"
        self.gw.ok("POST", "/v1/connector/follows/%s/wake" % other,
                   {"step": "intent", "wake_id": wake_id, "phase": "friends", "route": "exec"}, token)
        P.State(home).save("connector-ledger.json", {"v": 1, "wakes": {"%s:%s" % (bot, other): {
            "kind": "follow", "ref": other, "phase": "friends", "wake_id": wake_id, "step": "intended"}}})
        report = self.run_once(home, bot)
        self.assertEqual([{"ref": other, "outcome": "uncertain"}], report["recovered"])
        self.assertEqual([], report["woken"])
        self.assertEqual([], self.lines(log), "a wake that may have happened is not sent again")
        row = self.gw.sql("SELECT wake_state FROM follows WHERE src=? AND dst=? AND mutual_wake=1",
                          (other, bot))[0]
        self.assertEqual("uncertain", row["wake_state"])
        self.assertEqual([], self.listing(token, "mutual")["follows"])
        again = self.run_once(home, bot)
        self.assertEqual([], again["woken"])
        self.assertEqual([], again["recovered"])
        self.assertEqual([], self.lines(log))

    def test_friendships_from_before_the_upgrade_are_not_announced(self):
        bot, source, _, home, log, token = self.connector_for("upgrade", follows=True)
        old, old_source = self.gw.owned_bot(self.handle("oldfan"))
        self.assertEqual(201, self.gw.follow(source, old)[0])
        self.assertEqual(201, self.gw.follow(old_source, bot)[0])
        self.gw.stop()
        try:
            self.gw.sql("DELETE FROM meta WHERE key='follow_wakes'")
            self.gw.sql("UPDATE follows SET wake_state='none'")
        finally:
            self.gw.start()
        self.assertEqual([], self.listing(token, "mutual")["follows"])
        self.assertEqual([], self.run_once(home, bot)["woken"])
        self.assertEqual([], self.lines(log))
        fresh, fresh_source = self.gw.owned_bot(self.handle("newfan"))
        self.assertEqual(201, self.gw.follow(source, fresh)[0])
        self.assertEqual(201, self.gw.follow(fresh_source, bot)[0])
        self.assertEqual([fresh], [item["bot_id"] for item in self.listing(token, "mutual")["follows"]])
        self.assertEqual([fresh], [item["ref"] for item in self.run_once(home, bot)["woken"]])
        self.assertEqual(["yobiru.follow friends %s" % fresh], self.lines(log))


class OldGatewayFollowTests(unittest.TestCase):
    def drive(self, listing, bot=None, follows=False, old_gateway=False):
        tmp = tempfile.TemporaryDirectory(prefix="yobiru follow old gateway ")
        self.addCleanup(tmp.cleanup)
        st = P.State(Path(tmp.name))
        posts, gets = [], []
        bot = bot or bot_id("c")
        route = {"type": "manual", "wake_on_follows": follows}

        def request(base, method, path, body, token, timeout=15):
            if method == "GET":
                gets.append(path)
                if old_gateway and "follows=" in path:
                    return {"ok": False, "error": "query_not_supported", "http_status": 400}
                return listing
            posts.append((path, body.get("step"), body.get("phase")))
            return {"ok": True, "duplicate": False}

        args = SimpleNamespace(once=True, wait_seconds=25, idle_exit=None, poll_seconds=5)
        with patch.object(P, "request", side_effect=request), \
                patch.object(P, "perform_wake", return_value=("accepted", "manual")) as wake:
            P.connector_run(st, args, "http://127.0.0.1:9", bot, {"token": "t", "handle": "@old"}, route)
        return posts, wake, gets

    def test_the_switch_off_does_not_ask_and_does_not_wake(self):
        follower = bot_id("b")
        posts, wake, gets = self.drive({"ok": True, "jobs": [], "checks": [], "replies": [], "follows": [
            {"bot_id": follower, "revision": 1, "kind": "friends"}]})
        self.assertEqual(["/v1/connector/jobs"], gets)
        self.assertEqual([], posts)
        wake.assert_not_called()

    def test_an_old_gateway_that_rejects_follows_still_wakes_a_job(self):
        job = "msg_" + "a" * 16
        posts, wake, gets = self.drive({"ok": True, "jobs": [{"job_id": job, "phase": "ready"}],
                                        "checks": [], "replies": []}, follows=True, old_gateway=True)
        self.assertEqual(["/v1/connector/jobs?follows=mutual", "/v1/connector/jobs"], gets)
        self.assertEqual(1, wake.call_count)
        self.assertEqual("yobiru.job", wake.call_args[0][1]["type"])
        self.assertNotIn("follower_bot_id", wake.call_args[0][1])
        self.assertEqual([("/v1/connector/messages/%s/wake" % job, "intent"),
                          ("/v1/connector/messages/%s/wake" % job, "result")],
                         [(path, step) for path, step, _ in posts])

    def test_a_follow_entry_that_is_not_a_friendship_is_not_woken(self):
        follower = bot_id("d")
        posts, wake, gets = self.drive({"ok": True, "jobs": [], "checks": [], "replies": [], "follows": [
            {"bot_id": "@guoyu", "revision": 1, "kind": "friends"},
            {"bot_id": follower, "revision": 1},
            {"bot_id": follower, "revision": 1, "kind": "follow"},
            "nope", None]}, follows=True)
        self.assertEqual(["/v1/connector/jobs?follows=mutual"], gets)
        self.assertEqual([], posts)
        wake.assert_not_called()

    def test_wake_body_names_the_friend_id_and_no_name(self):
        follower = bot_id("b")
        owner = bot_id("a")
        body = P.wake_body("follow", "http://127.0.0.1:9", owner, "@hacken", follower, "friends", "w" * 12)
        self.assertEqual("yobiru.follow", body["type"])
        self.assertEqual(follower, body["follower_bot_id"])
        self.assertEqual("friends", body["phase"])
        self.assertNotIn("job_id", body)
        blob = json.dumps(body)
        self.assertNotIn("guoyu", blob)
        self.assertNotIn("Synthetic", blob)
        self.assertIn(owner, body["instructions"])
        self.assertIn("friends", body["instructions"])
        self.assertIn("owner followers", body["instructions"])
