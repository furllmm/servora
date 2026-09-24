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
...
