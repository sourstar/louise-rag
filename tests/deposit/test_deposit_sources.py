"""tests/deposit/test_deposit_sources.py — a deposit collection's URL sources
(06 §6.7, 04 §4.4; migration step 15e).

LocalRecall's behaviour, reproduced: one deposit per URL under LocalRecall's
own key, a page as text, a sitemap's pages joined, a repository's text
files concatenated, replaced whole on refresh, kept when a refresh fails.
Plus what Louise adds: nothing fetched before the node's acceptance (a keyed
git clone aside), every byte through the budget. The fetches go to a local
HTTP server and a local git repository — no network.
"""
import http.server
import json
import subprocess
import threading
from pathlib import Path

import pytest

from app import licensing
from app.acquire import budget
from app.hold import deposit, sources
from tests.deposit.test_deposit_identity import make, node  # noqa: F401

GOLDEN = json.loads((Path(__file__).parent / "data" / "sanitize_url_golden.json")
                    .read_text(encoding="utf-8"))


# ---- names ---------------------------------------------------------------

@pytest.mark.parametrize("url", sorted(GOLDEN))
def test_sanitize_url_is_localrecalls_byte_for_byte(url):
    """The golden values are the OUTPUT OF LOCALRECALL'S OWN FUNCTION,
    `sanitizeURL` copied verbatim from rag/source_manager.go at 4cae67b and
    run with Go — not what someone believes it does."""
    assert sources.sanitize_url(url) == GOLDEN[url]


def test_the_key_is_localrecalls_entry_name_and_fits_a_key():
    assert sources.source_key("alice", "https://site.example/sitemap.xml") == \
        "source-alice-https-site-example-sitemap-xml.txt"
    long = sources.source_key("alice", "https://example.org/" + "abc/" * 100)
    assert len(long.encode()) <= 255 and long.endswith(".txt")


# ---- a local origin -------------------------------------------------------

class _Site:
    def __init__(self, root: Path):
        self.root, self.hits, self.fail = root, [], set()
        site = self

        class H(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *a, **kw):
                super().__init__(*a, directory=str(site.root), **kw)

            def do_GET(self):
                site.hits.append(self.path)
                if self.path in site.fail:
                    self.send_error(500)
                    return
                super().do_GET()

            def log_message(self, *a):
                pass
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"


@pytest.fixture
def site(tmp_path):
    root = tmp_path / "www"
    root.mkdir()
    (root / "page.html").write_text(
        "<html><head><title>Ricette</title><style>.x{}</style></head><body>"
        "<h1>Carbonara</h1><p>Guanciale, pecorino, uova.</p>"
        "<script>track()</script></body></html>", encoding="utf-8")
    (root / "a.html").write_text("<p>Pagina A sul pane.</p>", encoding="utf-8")
    (root / "b.html").write_text("<p>Pagina B sul vino.</p>", encoding="utf-8")
    s = _Site(root)
    (root / "sitemap.xml").write_text(
        '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"<url><loc>{s.base}/a.html</loc></url><url><loc>{s.base}/b.html</loc></url>"
        f"<url><loc>{s.base}/missing.html</loc></url></urlset>", encoding="utf-8")
    yield s
    s.server.shutdown()


@pytest.fixture(autouse=True)
def running(node):  # noqa: F811
    """The node's mode: sources are fetched only where everything runs."""
    from app import node_state
    node_state.set_worker_state("running")


@pytest.fixture
def charged(monkeypatch):
    got = []
    monkeypatch.setattr(budget, "acquire_bytes", lambda n: got.append(n))
    return got


@pytest.fixture
def accepted(node):  # noqa: F811
    licensing.accept_web_sources("2026-09-30T00:00:00Z")


# ---- the gate (04 §4.4) ---------------------------------------------------

def test_before_the_acceptance_nothing_is_subscribed_or_fetched(node, site):  # noqa: F811
    make(node, "alice")
    with pytest.raises(sources.SourceError, match="web-sources disclaimer"):
        sources.add("alice", f"{site.base}/page.html")
    assert sources.subscriptions("alice") == []
    assert sources.tick() == 0 and site.hits == []


def test_the_acceptance_is_of_the_text_the_platform_ships(node, tmp_path, monkeypatch):  # noqa: F811
    licensing.accept_web_sources()
    assert licensing.web_sources_accepted()
    changed = tmp_path / "WEB-DISCLAIMER.txt"
    changed.write_bytes(licensing.WEB_DISCLAIMER.read_bytes() + b"\n5. A new clause.\n")
    monkeypatch.setattr(licensing, "WEB_DISCLAIMER", changed)
    assert not licensing.web_sources_accepted()     # a changed text asks again


@pytest.mark.parametrize("url,keyed", [
    ("git@github.com:owner/repo.git", True),
    ("ssh://git@host.example/repo.git", True),
    ("https://github.com/owner/repo.git", False),
    ("git@github.com:owner/repo", False),
    ("https://user@example.org/page", False),
])
def test_only_a_git_repository_over_ssh_is_keyed(url, keyed):
    assert licensing.is_keyed_git(url) is keyed


def test_a_keyed_clone_needs_no_acceptance_when_the_node_has_a_key(node, monkeypatch):  # noqa: F811
    from app import config
    assert licensing.web_source_refusal("git@host:o/r.git")          # no key: refused
    monkeypatch.setattr(config, "GIT_PRIVATE_KEY_FILE", "/keys/git_ed25519")
    assert licensing.web_source_refusal("git@host:o/r.git") is None
    assert licensing.web_source_refusal("https://host/o/r.git")      # not over ssh


# ---- what a URL gives ------------------------------------------------------

def test_a_page_is_one_deposit_of_its_text(node, site, accepted, charged):  # noqa: F811
    make(node, "alice")
    url = f"{site.base}/page.html"
    got = sources.add("alice", url, 30)
    assert got["key"] == sources.source_key("alice", url)
    assert sources.tick() == 1
    [key] = deposit.keys("alice")
    text = deposit.show("alice", key)["content"]
    assert "Carbonara" in text and "Guanciale" in text
    assert "track()" not in text and ".x{}" not in text
    assert sum(charged) == len((site.root / "page.html").read_bytes())   # invariant 5
    [row] = sources.listing("alice")
    assert row["url"] == url and row["update_interval"] == 30 and row["last_update"]


def test_a_sitemap_is_its_pages_joined_and_a_failed_page_left_out(node, site, accepted, charged):  # noqa: F811
    make(node, "alice")
    sources.add("alice", f"{site.base}/sitemap.xml")
    sources.tick()
    [key] = deposit.keys("alice")
    text = deposit.show("alice", key)["content"]
    assert "Pagina A sul pane." in text and "Pagina B sul vino." in text


def test_a_repository_is_its_text_files_concatenated(node, tmp_path, accepted, charged):  # noqa: F811
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("# Progetto\n\nIstruzioni.\n", encoding="utf-8")
    (repo / "src").mkdir()
    (repo / "src" / "main.py").write_text("print('ciao')\n", encoding="utf-8")
    (repo / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    git = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True,  # noqa: E731
                                    capture_output=True)
    git("init", "-q")
    git("-c", "user.email=t@t", "-c", "user.name=t", "add", ".")
    git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
    make(node, "alice")
    url = f"file://{repo}/.git"
    sources.add("alice", url)
    assert sources.tick() == 1
    text = deposit.show("alice", sources.source_key("alice", url))["content"]
    assert "--- File: README.md ---" in text and "Istruzioni." in text
    assert "--- File: src/main.py ---" in text and "PNG" not in text
    assert sum(charged) > 0


# ---- refreshing ------------------------------------------------------------

def test_a_source_is_refetched_when_its_interval_has_passed(node, site, accepted, charged):  # noqa: F811
    import time
    make(node, "alice")
    url = f"{site.base}/page.html"
    sources.add("alice", url, 60)
    t0 = time.time()
    sources.tick(now=t0)
    assert sources.tick(now=t0 + 59 * 60) == 0          # not due
    (site.root / "page.html").write_text("<p>Amatriciana, ora.</p>", encoding="utf-8")
    assert sources.tick(now=t0 + 61 * 60) == 1
    [key] = deposit.keys("alice")                        # replaced whole: still one
    assert "Amatriciana" in deposit.show("alice", key)["content"]


def test_a_failed_refresh_keeps_the_deposit_it_had(node, site, accepted, charged):  # noqa: F811
    import time
    make(node, "alice")
    url = f"{site.base}/page.html"
    sources.add("alice", url, 1)
    t0 = time.time()
    sources.tick(now=t0)
    site.fail.add("/page.html")
    assert sources.tick(now=t0 + 120) == 0
    [key] = deposit.keys("alice")
    assert "Carbonara" in deposit.show("alice", key)["content"]


def test_erasing_a_source_ends_the_subscription_and_erases_its_deposit(node, site, accepted,  # noqa: F811
                                                                       charged):
    make(node, "alice")
    url = f"{site.base}/page.html"
    sources.add("alice", url)
    sources.tick()
    deposit.add("alice", "note.txt", b"kept")
    sources.erase("alice", url)
    assert sources.subscriptions("alice") == []
    assert deposit.keys("alice") == ["note.txt"]
    held = deposit.held_file(node, "alice").read_text(encoding="utf-8")
    assert 'embed = "all"' in held and "[encoder]" in held     # the rest carried through


def test_the_same_url_twice_is_refused_as_localrecall_refuses_it(node, site, accepted):  # noqa: F811
    make(node, "alice")
    sources.add("alice", f"{site.base}/page.html")
    with pytest.raises(sources.SourceError, match="already exists"):
        sources.add("alice", f"{site.base}/page.html")


def test_the_command_line(node, site, charged, capsys):  # noqa: F811
    from app import louise
    make(node, "alice")
    assert louise.main(["hold", "collection", "ack", "--web-sources"]) == 0
    assert "property of its owners" in capsys.readouterr().out     # printed, then recorded
    assert licensing.web_sources_accepted()
    url = f"{site.base}/page.html"
    assert louise.main(["hold", "source", "add", "alice", url, "--interval", "15",
                        "--here"]) == 0
    capsys.readouterr()
    sources.tick()
    assert louise.main(["hold", "source", "list", "alice", "--json"]) == 0
    [row] = json.loads(capsys.readouterr().out)
    assert row["update_interval"] == 15 and row["last_update"]
    assert louise.main(["hold", "source", "erase", "alice", url, "--here"]) == 2   # --yes
    assert louise.main(["hold", "source", "erase", "alice", url, "--yes", "--here"]) == 0
    assert deposit.keys("alice") == []


@pytest.mark.parametrize("mode", ["paused", "seed-only", "acquire-only"])
def test_a_node_not_running_everything_fetches_no_source(node, site, accepted, mode):  # noqa: F811
    from app import node_state
    make(node, "alice")
    sources.add("alice", f"{site.base}/page.html")
    node_state.set_worker_state(mode)
    assert sources.tick() == 0 and site.hits == []
    node_state.set_worker_state("running")
    assert sources.tick() == 1
