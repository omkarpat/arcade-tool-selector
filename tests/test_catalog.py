import os
import subprocess
import sys

from mcp import types

from arcade_tool_selector.catalog import Catalog, CatalogStore, diff


def tool(name: str, description: str = "d") -> types.Tool:
    return types.Tool(name=name, description=description, input_schema={"type": "object", "properties": {}})


def test_diff_and_round_trip(tmp_path):
    store = CatalogStore(tmp_path, "gw")
    old = Catalog.from_tools([tool("Gmail_ListEmails"), tool("Linear_CreateIssue")])
    store.save(old)
    assert store.load().digest == old.digest

    new = Catalog.from_tools([tool("Gmail_ListEmails", "changed"), tool("Slack_SendMessage")])
    changes = diff(store.load(), new)
    assert (changes.added, changes.removed, changes.changed) == (["Slack_SendMessage"], ["Linear_CreateIssue"], ["Gmail_ListEmails"])
    assert diff(old, old).empty


def test_lease_one_holder_until_expiry_or_exit(tmp_path):
    store = CatalogStore(tmp_path, "gw")
    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert store.acquire_lease(ttl=60, pid=holder.pid)
        assert not store.acquire_lease(ttl=60)
        assert store.acquire_lease(ttl=60, pid=holder.pid)  # holder renews
    finally:
        holder.kill()
        holder.wait()
    assert store.acquire_lease(ttl=60)  # holder exited
    assert store.acquire_lease(ttl=-1)
    assert store.acquire_lease(ttl=60, pid=os.getpid() + 100000)  # expired lease is free
