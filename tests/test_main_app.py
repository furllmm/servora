from servora.apps import validate_app_manifest


def test_app_manifest_has_expected_stack_fields():
    m = validate_app_manifest({
        'name': 'stack', 'version': '1',
        'services': [
            {'name': 'db', 'image': 'postgres:16', 'volumes': [{'name': 'db-data', 'container_path': '/var/lib/postgresql/data'}]},
            {'name': 'web', 'image': 'nginx:alpine', 'depends_on': ['db'], 'networks': ['stack-net']},
        ]
    })
    assert m.services[1].depends_on == ['db']
    assert m.services[0].volumes[0].name == 'db-data'
