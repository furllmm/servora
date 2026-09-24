from servora.marketplace import MarketplaceError, MarketplaceStore, validate_entry

def entry(name="demo"):
    return {"name": name, "category": "community", "verification": "community", "description": "Demo",
            "tags": ["web"], "source": {"type": "oci", "image": "nginx:alpine"},
            "manifest": {"name": name, "version": "1.0.0", "description": "Demo",
                         "services": [{"name": "web", "image": "nginx:alpine"}]}}

def test_marketplace_publish_and_download(tmp_path):
    store = MarketplaceStore(tmp_path)
    store.publish(entry())
    package = store.download("demo")
    assert package["format"] == "servora-marketplace-v1"
    assert package["entry"]["manifest"]["name"] == "demo"

def test_marketplace_fork_creates_community_copy(tmp_path):
    store = MarketplaceStore(tmp_path)
    store.save({**entry("officialish"), "category": "official", "verification": "verified"})
    fork = store.fork("officialish", "my-copy")
    assert fork.category == "community"
    assert fork.manifest["name"] == "my-copy"
    assert fork.source["forked_from"] == "officialish"
    assert "fork" in fork.tags

def test_marketplace_rejects_duplicate_fork(tmp_path):
    store = MarketplaceStore(tmp_path)
    store.publish(entry())
    store.publish(entry("other"))
    try:
        store.fork("demo", "other")
    except MarketplaceError as exc:
        assert "already exists" in str(exc)
    else:
        raise AssertionError("duplicate fork should fail")

def test_marketplace_entry_validation(tmp_path):
    validated = validate_entry(entry())
    assert validated.manifest["name"] == "demo"
