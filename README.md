# Servora

> Linux-first, lightweight and portable Podman homelab manager — a management layer, not an OS or VM.

Servora provides a simple UI and API for managing Podman containers, images, volumes, networks, applications, marketplace manifests, and AI-assisted container setup. It is designed for lightweight Linux systems, portable runtimes, and beginner-friendly homelab management without requiring a virtual machine.

**Status:** Active development · **Current:** 0.1.12

## Highlights
- Podman backend with container, image, volume, network, logs, inspect, stats, and lifecycle operations
- Portable and user runtime modes
- App manifest system with install, uninstall, update, dependencies, volumes, networks, ports, and transactional rollback journals
- Reliability scanning for corrupted state, missing/orphaned managed containers, runtime health, and operation history
- Local marketplace foundation with `official`, `community`, and `ai_imported` entries
- AI import pipeline with manifest validation and security risk scanning
- OpenAI-compatible local/cloud AI provider abstraction
- Structured AI plans instead of raw shell access
- Minimal web management UI with Create Container with AI plan preview and approval flow
- Bare Server Browser abstraction for future embedded container web-app rendering

## Architecture
Servora is intentionally a management layer rather than an operating system:

```text
Servora
├── Management UI / API
├── Podman Backend
├── Runtime
├── App System
├── Marketplace
├── Server Browser
└── AI Assistant
```

## Run
```bash
python3 -m servora.main
```
Then open `http://127.0.0.1:8787`.

## Tests
```bash
python3 -m pytest -q
```

## App Marketplace
Marketplace entries are Servora manifests plus source metadata. Categories currently include:
- `official`
- `community`
- `ai_imported`

The local foundation supports listing/search, manifest inspection, publishing, installation, and **Fork & Edit**. Marketplace content is validated by Servora rather than blindly executed.

## AI Import
The AI-import pipeline can convert supported sources into a Servora manifest and run a security risk scan before publication.

Supported source types:
- `oci_image` — OCI/Podman image reference such as `nginx:alpine`
- `compose` — Docker Compose data
- `manifest` — existing Servora manifest

Risk checks include privileged mode, host networking, host mounts, container-engine sockets, devices, extra capabilities, custom security options, shell commands, missing resource limits, and privileged host ports.

Medium/high/critical findings require explicit approval. The AI layer is not granted raw shell access.

## AI Provider
Servora includes a dependency-free OpenAI-compatible provider for local or cloud endpoints.

Environment variables:
- `SERVORA_AI_PROVIDER`
- `SERVORA_AI_BASE_URL`
- `SERVORA_AI_MODEL`
- `SERVORA_AI_API_KEY`

The safe default is the local `mock` provider.

API:
```text
POST /api/ai/plan
POST /api/ai/import
POST /api/ai/create
```

## Roadmap
- App repository synchronization
- Health checks and recovery workflows
- Export/import bundles
- Embedded Bare Server Browser renderer
- LAN/WAN access modes
- Linux packaging and portable releases

## License
See the repository license file.
