import pytest
from servora.apps import AppManifestError, manifest_to_dict, service_container_name, validate_app_manifest, resolve_service_order


def test_manifest_roundtrip():
    m = validate_app_manifest({
        'name': 'demo', 'version': '1',
        'services': [{'name': 'web', 'image': 'nginx:alpine',
                      'ports': [{'host': 8081, 'container': 80}],
                      'volumes': [{'name': 'demo-data', 'container_path': '/data'}],
                      'environment': {'MODE': 'test'}, 'networks': ['demo-net']}],
    })
    assert m.name == 'demo'
    assert manifest_to_dict(m)['services'][0]['volumes'][0]['name'] == 'demo-data'


def test_manifest_rejects_bad_env_and_dependency():
    with pytest.raises(AppManifestError):
        validate_app_manifest({'name':'demo','services':[{'name':'web','image':'nginx','environment':{'BAD-NAME':'x'}}]})
    with pytest.raises(AppManifestError):
        validate_app_manifest({'name':'demo','services':[{'name':'web','image':'nginx','depends_on':['missing']}]})


def test_container_name():
    assert service_container_name('demo', 'web') == 'servora-demo-web'

from servora.apps import AppStore, uninstall_app, update_app


def test_app_store_persists(tmp_path):
    store = AppStore(tmp_path)
    manifest = validate_app_manifest({"name":"demo","version":"1.0","services":[{"name":"web","image":"nginx:alpine"}]})
    store.save(manifest)
    assert store.get("demo").version == "1.0"
    assert [x.name for x in store.list()] == ["demo"]
    store.remove("demo")
    assert store.get("demo") is None


class LifecyclePodman:
    def __init__(self): self.removed=[]; self.created=[]
    def list_networks(self): return []
    def list_volumes(self): return []
    def create_network(self, n): return n
    def create_volume(self, n): return n
    def create_container(self, plan): self.created.append(plan["name"]); return "id"
    def remove_container(self, name, force=False): self.removed.append((name, force)); return name
    def remove_volume(self, name, force=False): return name
    def remove_network(self, name): return name


def test_uninstall_preserves_data_by_default():
    p = LifecyclePodman()
    m = validate_app_manifest({"name":"demo","version":"1.0","services":[{"name":"web","image":"nginx","volumes":["data:/data"],"networks":["net"]}]})
    result = uninstall_app(p, m)
    assert result["removed_containers"] == ["servora-demo-web"]
    assert result["removed_volumes"] == []


def test_update_keeps_same_app_name(tmp_path):
    p = LifecyclePodman()
    store = AppStore(tmp_path)
    old = {"name":"demo","version":"1.0","services":[{"name":"web","image":"nginx:1"}]}
    new = {"name":"demo","version":"2.0","services":[{"name":"web","image":"nginx:2"}]}
    old_m = validate_app_manifest(old); store.save(old_m)
    result = update_app(p, old_m, new, store=store, check_ports=False)
    assert result["updated"] is True
    assert store.get("demo").version == "2.0"

from servora.marketplace import MarketplaceError, MarketplaceStore, validate_entry


def test_marketplace_entry_and_search(tmp_path):
    store = MarketplaceStore(tmp_path)
    entry = {
        "name": "demo-app",
        "category": "community",
        "verification": "community",
        "description": "Demo app",
        "tags": ["demo", "web"],
        "source": {"type": "oci", "image": "nginx:alpine"},
        "manifest": {"name": "demo-app", "version": "1.0", "services": [{"name": "web", "image": "nginx:alpine"}]},
    }
    store.publish(entry)
    assert store.get("demo-app").version == "1.0"
    assert [x.name for x in store.list(query="WEB")] == ["demo-app"]


def test_marketplace_rejects_official_publish():
    with __import__('pytest').raises(MarketplaceError):
        MarketplaceStore("/tmp/servora-test-marketplace").publish({"name":"official-app", "category":"official", "verification":"verified", "manifest":{"name":"official-app","version":"1","services":[{"name":"web","image":"nginx"}]}})


def test_app_health_aggregates_services():
    from servora.apps import app_health

    class HealthPodman:
        def container_health(self, name):
            return {"name": name, "status": "healthy", "running": True, "healthcheck": "healthy"}

    m = validate_app_manifest({"name":"demo","version":"1","services":[
        {"name":"db","image":"postgres"},
        {"name":"web","image":"nginx","depends_on":["db"]},
    ]})
    result = app_health(HealthPodman(), m)
    assert result["status"] == "healthy"
    assert [x["service"] for x in result["services"]] == ["db", "web"]


def test_dependency_order_is_deterministic():
    m = validate_app_manifest({"name":"demo","version":"1","services":[
        {"name":"web","image":"nginx","depends_on":["db"]},
        {"name":"db","image":"postgres"},
        {"name":"cache","image":"redis"},
    ]})
    assert [s.name for s in resolve_service_order(m)] == ["cache", "db", "web"]


def test_dependency_cycle_reports_path():
    m = validate_app_manifest({"name":"demo","version":"1","services":[
        {"name":"web","image":"nginx","depends_on":["db"]},
        {"name":"db","image":"postgres","depends_on":["web"]},
    ]})
    with pytest.raises(AppManifestError, match="web -> db -> web"):
        resolve_service_order(m)
