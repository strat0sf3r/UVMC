"""Projects and privacy levels: which model may see which files (privacy.py, docs/design.md 7k)."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ["UVMC_NO_SUDO"] = "1"
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent))

from fastapi.testclient import TestClient  # noqa: E402

from test_conversations import ConversationsTest, run, say  # noqa: E402
from test_service import ServiceBase, wait_for  # noqa: E402
from uvmc import privacy  # noqa: E402
from uvmc.privacy import GENERAL, NeedsConfirm, PrivacyError, Projects, Scope  # noqa: E402
from uvmc.service import create_app  # noqa: E402

H = {"Authorization": "Bearer t"}


class ProjectsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.p = Projects(Path(self.tmp.name) / "projects.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_shares_are_projects_with_one_level(self):
        self.assertTrue(self.p.has("share:photos"))
        self.assertFalse(self.p.has("share:Bad Name"))
        self.assertEqual(self.p.project_level("share:photos"), "local")   # local until you set it
        self.p.set_level("share:photos", "paid")
        self.assertEqual((self.p.lowest("share:photos"), self.p.label("share:photos")), ("paid", "the shared folder photos"))
        self.assertTrue(self.p.describe()["shares"]["photos"]["asked"])
        with self.assertRaises(ValueError):
            self.p.add("share:photos", "open")                          # not a folder name
        s = Scope(self.p, ["share:photos", "share:music"], "paid")
        self.assertEqual(s.shares(), ["photos"])                         # music: local, above paid
        self.assertEqual(s.shares(["music"]), [])                        # photos: not shared right now
        self.assertFalse(s.visible("share:photos/a.txt"))                # never a path in the main folder

    def test_levels_zones_and_marks(self):
        p = self.p
        p.add("Website", "paid")
        p.set_zone("Website/drafts", "open")
        p.set_zone("Website/keys", "local")
        self.assertEqual(p.project_of("Website/a.txt"), "Website")
        self.assertEqual(p.project_of("notes.txt"), GENERAL)          # nothing claims it: General
        self.assertEqual(p.level("notes.txt"), "local")               # General is local until you choose
        self.assertEqual(p.level("Website/a.txt"), "paid")
        self.assertEqual(p.level("Website/drafts/x.md"), "open")
        self.assertEqual(p.level("Website/keys/k"), "local")
        self.assertEqual(p.lowest("Website"), "open")
        # A mark only makes a file stricter.
        p.set_mark("Website/contract.pdf", "local")
        self.assertEqual(p.level("Website/contract.pdf"), "local")
        with self.assertRaises(ValueError):
            p.set_mark("Website/keys/k", "paid")
        with self.assertRaises(ValueError):
            p.set_mark("Website/a.txt", "open")
        # Kept by the supervisor, across restarts.
        again = Projects(Path(self.tmp.name) / "projects.json")
        self.assertEqual(again.level("Website/contract.pdf"), "local")
        self.assertEqual(again.level("Website/drafts/x.md"), "open")

    def test_the_one_check(self):
        p = self.p
        p.add("Website", "paid")
        p.set_zone("Website/keys", "local")
        s = Scope(p, ["Website"], "paid")
        self.assertTrue(s.visible("Website/a.txt"))
        self.assertFalse(s.visible("Website/keys/k"))                 # more private than the conversation
        self.assertFalse(s.visible("other.txt"))                      # not one of its projects
        self.assertTrue(Scope(p, ["Website"], "local").visible("Website/keys/k"))
        held = Scope(p, ["Website"], "local", seen="local")
        self.assertTrue(held.held("Website/a.txt"))                   # local data into a paid place: you decide
        self.assertFalse(held.held("Website/keys/k2"))

    def test_bad_names(self):
        for bad in ("", ".hidden", "a/b", ".uvmc-merge"):
            with self.assertRaises(ValueError):
                self.p.add(bad, "open")
        with self.assertRaises(ValueError):
            self.p.set_zone("../x", "open")

    def test_suggestions(self):
        self.assertEqual(privacy.suggest("openai", "http://{pc}:8091/v1"), "local")
        self.assertEqual(privacy.suggest("anthropic", ""), "paid")
        self.assertEqual(privacy.suggest("claude-code", ""), "paid")
        self.assertEqual(privacy.suggest("openai", "https://api.deepseek.com"), "open")


class PrivacyControllerTest(ServiceBase):
    """Through the controller: copies, bring-in, model switches. The shipped profiles: claude
    (paid), local (local), deepseek (open)."""

    controller = ConversationsTest.controller   # a fake model that answers by the first task

    def setUp(self):
        super().setUp()
        self.cfg.privacy_general = "local"       # the shipped default
        w = Path(self.cfg.workdir)
        (w / "notes.txt").write_text("general\n")
        (w / "Website" / "keys").mkdir(parents=True)
        (w / "Website" / "drafts").mkdir()
        (w / "Website" / "index.html").write_text("<h1>hi</h1>\n")
        (w / "Website" / "drafts" / "post.md").write_text("draft\n")
        (w / "Website" / "keys" / "secret.txt").write_text("s3cret\n")

    def make(self, scripts):
        ctl = self.controller(scripts)
        ctl.projects.add("Website", "paid")
        ctl.projects.set_zone("Website/keys", "local")
        ctl.projects.set_zone("Website/drafts", "open")
        return ctl

    def work(self, ctl, c, tag):
        ctl.start_task(tag, c.id)
        self.assertTrue(wait_for(lambda: not c.busy, 10))

    def test_new_sites_asked_each_time_once_private_files_are_seen(self):
        import threading
        ctl = self.make({})
        c = ctl.conv()
        for always, expect in ((True, "always"), (False, "allow")):
            got = {}
            t = threading.Thread(target=lambda: got.setdefault("a", ctl._ask("site", {"host": "x.org", "always": always}, [c])))
            t.start()
            self.assertTrue(wait_for(lambda: ctl.pending))
            ctl.decide(next(iter(ctl.pending)), "always")
            t.join(5)
            self.assertEqual(got["a"], expect)     # no "always" for a conversation that has seen private files
        c.seen = "paid"
        text = ctl._live_briefing(ctl.settings.briefing(), c=c)
        self.assertIn("approval each time", text)

    def test_paid_model_gets_a_scratch_project_not_general(self):
        ctl = self.make({"alpha": [run("a1", "ls -R > seen.txt; echo x > report.txt"), say("ok")]})
        c = ctl.new_conversation("claude")
        self.assertEqual(c.level, "paid")
        self.assertEqual(c.projects, [f"scratch-{c.id}"])            # General is local: not for a paid model
        self.work(ctl, c, "alpha")
        copy = Path(c.copy_dir)
        self.assertFalse((copy / "notes.txt").exists())
        self.assertFalse((copy / "Website").exists())
        # Its top-level file can't be brought in: outside its projects.
        plan = ctl.changes(c.id)
        self.assertIn("report.txt", [x["path"] for x in plan["blocked"]])
        ctl.sync(c.id)
        self.assertFalse((Path(self.cfg.workdir) / "report.txt").exists())

    def test_hidden_zone_and_count(self):
        ctl = self.make({"alpha": [run("a1", "mkdir -p Website/keys; echo new > Website/keys/other.txt; "
                                             "echo ok >> Website/index.html"),
                                   say("ok")]})
        c = ctl.new_conversation("claude", projects=["Website"])
        self.work(ctl, c, "alpha")
        copy = Path(c.copy_dir)
        self.assertTrue((copy / "Website" / "index.html").exists())
        self.assertTrue((copy / "Website" / "drafts" / "post.md").exists())
        self.assertFalse((copy / "Website" / "keys" / "secret.txt").exists())
        self.assertIn("1 file is hidden", c.session.system)        # told how many, never which
        self.assertNotIn("secret", c.session.system)
        r = ctl.sync(c.id)
        self.assertIn("Website/index.html", r["brought_in"])
        self.assertIn("Website/keys/other.txt", r["blocked"])        # never into a place it can't see
        self.assertFalse((Path(self.cfg.workdir) / "Website" / "keys" / "other.txt").exists())
        self.assertEqual(c.seen, "paid")

    def test_local_model_changes_are_held_until_released(self):
        ctl = self.make({"alpha": [run("a1", "cat Website/keys/secret.txt >> Website/index.html; "
                                             "echo k >> Website/keys/secret.txt"), say("ok")]})
        c = ctl.new_conversation("local", projects=["Website"])
        self.assertEqual(c.level, "local")
        self.work(ctl, c, "alpha")
        self.assertEqual(c.seen, "local")
        plan = ctl.changes(c.id)
        self.assertEqual([x["path"] for x in plan["held"]], ["Website/index.html"])
        r = ctl.sync(c.id)
        self.assertIn("Website/keys/secret.txt", r["brought_in"])    # local into local: fine
        self.assertEqual(r["held"], ["Website/index.html"])
        main = Path(self.cfg.workdir) / "Website" / "index.html"
        self.assertNotIn("s3cret", main.read_text())
        self.assertIn("s3cret", ctl.held_diff(c.id, "Website/index.html")["diff"])
        r = ctl.sync(c.id, {"Website/index.html": "release"})       # you looked and let it through
        self.assertIn("Website/index.html", r["brought_in"])
        self.assertIn("s3cret", main.read_text())
        self.assertEqual(ctl.changes(c.id)["held"], [])

    def test_local_model_at_open_level_brings_in_freely(self):
        ctl = self.make({"alpha": [run("a1", "echo more >> Website/drafts/post.md"), say("ok")]})
        c = ctl.new_conversation("local", projects=["Website"], level="open")
        self.work(ctl, c, "alpha")
        self.assertFalse((Path(c.copy_dir) / "Website" / "index.html").exists())
        r = ctl.sync(c.id)
        self.assertEqual(r["brought_in"], ["Website/drafts/post.md"])
        self.assertEqual(r["held"], [])

    def test_levels_and_projects_are_checked_on_open(self):
        ctl = self.make({})
        with self.assertRaises(PrivacyError):
            ctl.new_conversation("claude", level="local")             # above its model
        with self.assertRaises(PrivacyError):
            ctl.new_conversation("claude", projects=[GENERAL])        # General is local
        ctl.projects.add("Taxes", "local")
        with self.assertRaises(PrivacyError):
            ctl.new_conversation("deepseek", projects=["Taxes"])
        c = ctl.new_conversation("deepseek", projects=["Website"])   # its open zone is enough
        self.assertEqual(c.level, "open")

    def test_switching_to_a_less_safe_model(self):
        ctl = self.make({"alpha": [run("a1", "cat Website/keys/secret.txt"), say("ok")],
                         "beta": [run("b1", "cat Website/index.html"), say("ok")]})
        a = ctl.new_conversation("local", projects=["Website"])
        self.work(ctl, a, "alpha")
        with self.assertRaises(PrivacyError):                         # it has seen local files
            ctl.set_model("claude", a.id)
        self.assertEqual(a.model, "local")
        b = ctl.new_conversation("local", projects=["Website"], level="paid")
        self.work(ctl, b, "beta")
        with self.assertRaises(NeedsConfirm):                         # allowed, once you confirm
            ctl.set_model("claude", b.id)
        ctl.set_model("claude", b.id, confirm=True)
        self.assertEqual((b.model, b.level), ("claude", "paid"))
        ctl.set_model("local", b.id)                                  # safer: no question
        self.assertEqual(b.level, "paid")                             # it stays where you put it
        # Not started yet and nothing chosen: it simply follows the model.
        fresh = ctl.new_conversation("claude")
        ctl.set_model("local", fresh.id)
        self.assertEqual((fresh.level, fresh.projects), ("local", [GENERAL]))

    def test_new_top_level_folder_becomes_a_project(self):
        ctl = self.make({"alpha": [run("a1", "mkdir Blog && echo hi > Blog/one.md"), say("ok")]})
        c = ctl.new_conversation("claude")
        self.work(ctl, c, "alpha")
        r = ctl.sync(c.id)
        self.assertIn("Blog/one.md", r["brought_in"])
        self.assertEqual(ctl.projects.project_level("Blog"), "paid")  # the model's level
        self.assertIn("Blog", c.projects)

    def test_workers_and_resolvers_must_be_as_safe(self):
        ctl = self.make({"alpha": [run("a1", "cat Website/keys/secret.txt"), say("ok")]})
        c = ctl.new_conversation("local", projects=["Website"])
        self.work(ctl, c, "alpha")
        with self.assertRaises(PrivacyError):
            ctl.set_worker("claude", c.id)
        o = ctl.new_conversation("claude", projects=["Website"])
        with self.assertRaises(PrivacyError):
            ctl._check_handover(c, o, "Website/index.html")

    def test_scratch_project_goes_when_unused(self):
        ctl = self.make({"alpha": [run("a1", "true"), say("ok")]})
        c = ctl.new_conversation("claude")
        self.work(ctl, c, "alpha")
        name = f"scratch-{c.id}"
        self.assertTrue(ctl.projects.has(name))
        self.assertTrue((Path(self.cfg.workdir) / name).is_dir())
        ctl.close_conversation(c.id)
        self.assertFalse(ctl.projects.has(name))
        self.assertFalse((Path(self.cfg.workdir) / name).exists())

    def test_claude_apps_on_the_pc_see_what_a_paid_model_may(self):
        from uvmc import mcp_http
        ctl = self.make({})
        read = lambda path: mcp_http.call(ctl, "read_file", {"path": path, "conversation": ""})  # noqa: E731
        listing = mcp_http.call(ctl, "list_files", {"path": "Website", "conversation": ""})
        text = listing["content"][0]["text"]
        self.assertIn("index.html", text)
        self.assertNotIn("keys", text)                                 # a local zone
        self.assertTrue(read("Website/keys/secret.txt")["isError"])
        self.assertTrue(read("notes.txt")["isError"])                  # General is local
        self.assertFalse(read("Website/index.html").get("isError"))

    def test_give_one_file(self):
        ctl = self.make({"alpha": [run("a1", "cat Website/keys/secret.txt > Website/notes.md"), say("ok")]})
        c = ctl.new_conversation("claude", projects=["Website"])
        with self.assertRaises(NeedsConfirm):                          # you're told what it means first
            ctl.give_file(c.id, "Website/keys/secret.txt")
        r = ctl.give_file(c.id, "Website/keys/secret.txt", confirm=True)
        self.assertEqual((r["given"], r["seen"]), (["Website/keys/secret.txt"], "local"))
        self.assertEqual(ctl.projects.level("Website/keys/secret.txt"), "local")   # it keeps its level
        self.work(ctl, c, "alpha")
        self.assertIn("gave you: Website/keys/secret.txt", c.session.system)
        self.assertEqual([x["path"] for x in ctl.changes(c.id)["held"]], ["Website/notes.md"])
        with self.assertRaises(ValueError):                            # it has it already
            ctl.give_file(c.id, "Website/keys/secret.txt", confirm=True)

    def test_start_with_another_conversations_transcript(self):
        ctl = self.make({"alpha": [run("a1", "cat Website/index.html"), say("Alpha looked.")],
                         "beta": [run("b1", "ls .uvmc-context"), say("Beta read it.")]})
        a = ctl.new_conversation("claude", projects=["Website"])
        a.title = "alpha work"
        self.work(ctl, a, "alpha")
        with self.assertRaises(PrivacyError):                          # an open one may not read a paid history
            ctl.new_conversation("deepseek", projects=["Website"], transcript_from=a.id)
        b = ctl.new_conversation("claude", projects=["Website"], transcript_from=a.id)
        self.work(ctl, b, "beta")
        ctx = list((Path(b.copy_dir) / ".uvmc-context").iterdir())
        self.assertEqual(len(ctx), 1)
        self.assertIn("Alpha looked.", ctx[0].read_text())
        self.assertIn(".uvmc-context/conversation-alpha-work.md", b.session.system)
        self.assertEqual(ctl.changes(b.id)["blocked"], [])             # it isn't a change to bring in

    def test_levels_in_file_lists(self):
        ctl = self.make({})
        c = ctl.new_conversation("claude", projects=["Website"])
        d = ctl.list_files("Website", c.id, "shared")
        e = {x["name"]: x for x in d["entries"]}
        self.assertEqual((e["keys"]["level"], e["keys"]["hidden"]), ("local", True))
        self.assertEqual((e["drafts"]["level"], e["drafts"]["zone"]), ("open", "open"))
        self.assertFalse(e["index.html"]["hidden"])
        self.assertEqual(d["privacy"]["project"], "Website")
        f = ctl.read_file("Website/keys/secret.txt", conv=c.id, where="shared")
        self.assertEqual((f["level"], f["hidden"], f["rel"]), ("local", True, "Website/keys/secret.txt"))

    def test_api(self):
        ctl = self.make({})
        client = TestClient(create_app(ctl, "t"))
        r = client.post("/api/conversations", json={"model": "claude", "projects": [GENERAL]}, headers=H)
        self.assertEqual(r.status_code, 403)
        self.assertTrue(r.json()["privacy"])
        r = client.post("/api/projects/level", json={"name": "Website", "level": "open"}, headers=H)
        self.assertEqual(r.status_code, 409)                          # lowering asks first
        self.assertTrue(r.json()["confirm"])
        self.assertIn("will be able to see it", r.json()["detail"])
        r = client.post("/api/projects/level", json={"name": "Website", "level": "open", "confirm": True}, headers=H)
        self.assertEqual(r.status_code, 200)
        r = client.post("/api/projects/mark", json={"path": "Website/index.html", "level": "local"}, headers=H)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["marks"], {"Website/index.html": "local"})
        r = client.post("/api/projects", json={"name": "Taxes", "level": "local"}, headers=H)
        self.assertEqual(r.status_code, 200)
        self.assertTrue((Path(self.cfg.workdir) / "Taxes").is_dir())
        view = client.get("/api/projects", headers=H).json()
        self.assertEqual({p["label"]: p["level"] for p in view["projects"]},
                         {"General": "local", "Taxes": "local", "Website": "open"})
        r = client.post("/api/conversations", json={"model": "local", "projects": ["Taxes", "Website"]}, headers=H)
        self.assertEqual(r.status_code, 200)
        conv = next(x for x in r.json()["conversations"] if x["id"] == r.json()["conv"])
        self.assertEqual([p["name"] for p in conv["projects"]], ["Taxes", "Website"])
        self.assertEqual(conv["level"], "local")
        same = client.post("/api/conversations", json={"like": r.json()["conv"]}, headers=H).json()
        conv2 = next(x for x in same["conversations"] if x["id"] == same["conv"])
        self.assertEqual([p["name"] for p in conv2["projects"]], ["Taxes", "Website"])


if __name__ == "__main__":
    unittest.main()
