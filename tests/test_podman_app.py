from servora.podman import Podman

class FakePodman(Podman):
    def __init__(self):
        self.executable = 'podman'
        self.env = {}
        self.calls = []
    def _run(self, *args, **kwargs):
        self.calls.append(args)
        class R: stdout='abc\n'; stderr=''; returncode=0
        return R()
    def list_networks(self): return []
    def list_volumes(self): return []
    def create_network(self, name): self.calls.append(('network-create', name)); return name
    def create_volume(self, name): self.calls.append(('volume-create', name)); return name
    def remove_container(self, name, force=False): self.calls.append(('rm', name, force)); return name


def test_create_container_builds_safe_argument_list():
    p=FakePodman()
    p.create_container({'name':'servora-demo-web','image':'nginx:alpine',
                        'ports':[{'host':8081,'container':80,'protocol':'tcp'}],
                        'volumes':[{'name':'data','container_path':'/data','read_only':True}],
                        'environment':{'MODE':'test'},'networks':['demo'],'command':['-g','daemon off;']})
    call=p.calls[0]
    assert call[:3] == ('create','--name','servora-demo-web')
    assert '-p' in call and '8081:80/tcp' in call
    assert '-v' in call and 'data:/data:ro' in call
    assert '-e' in call and 'MODE=test' in call
    assert '--network' in call and 'demo' in call
