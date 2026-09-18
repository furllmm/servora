DEMO = {
    "name": "nginx-demo",
    "category": "official",
    "verification": "verified",
    "description": "Small example web service for testing Servora app lifecycle.",
    "tags": ["web", "demo"],
    "source": {"type": "oci", "image": "nginx:alpine"},
    "manifest": {
        "name": "nginx-demo",
        "version": "1.0.0",
        "description": "Small example web service for testing Servora app lifecycle.",
        "services": [{
            "name": "web",
            "image": "nginx:alpine",
            "ports": [{"host": 8081, "container": 80}],
        }],
    },
}
