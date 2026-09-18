from dataclasses import dataclass
import socket
from .podman import Podman, PodmanError

class ServiceError(RuntimeError): pass
@dataclass
class LaunchResult:
    container_id: str
    name: str
    started: bool
    health: str

def ensure_port_is_available(port, host='127.0.0.1'):
    with socket.socket() as s:
        s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
        try: s.bind((host,port))
        except OSError as e: raise ServiceError(f'Port {port} is unavailable') from e

def create_container(podman, plan, check_ports=True):
    for p in plan.get('ports',[]):
        if check_ports: ensure_port_is_available(int(p['host']))
    return podman.create_container(plan)
