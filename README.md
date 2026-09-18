# Servora

Linux-first, lightweight and portable Podman homelab manager. Servora is a management layer, not an OS or VM.

## 0.1.4

- App manifest validation
- App install lifecycle with dependency ordering and rollback cleanup
- Persistent installed-app registry under the Servora runtime
- App list/get API
- Uninstall with data-preserving defaults
- Optional volume/network cleanup
- Best-effort update with automatic old-manifest restore on install failure
- Web UI for installed apps, manifest loading, install/update and uninstall
- 16 automated tests

## Run

```bash
python3 -m servora.main
```

Then open `http://127.0.0.1:8787`.

## Tests

```bash
python3 -m pytest -q
```

## Marketplace (0.1.5)

Servora now has a local marketplace registry with three publishing categories:

- `official`
- `community`
- `ai_imported`

Entries contain a Servora manifest plus source metadata, tags, and a verification label. The local registry supports listing/search, viewing manifests, and community publishing. Marketplace entries are metadata/manifests only; Servora does not blindly execute marketplace content.

API endpoints:

- `GET /api/marketplace`
- `GET /api/marketplace/get?name=<app>`
- `POST /api/marketplace/publish`

The UI exposes **View manifest**, **Install**, and **Fork & Edit**. Network synchronization and a remote public repository are intentionally separate from this local foundation.

## 0.1.6 — AI Import pipeline

The local AI-import foundation now converts supported sources into a Servora manifest and runs a risk scan before marketplace publication.

Supported source types:

- `oci_image` — an OCI/Podman image reference such as `nginx:alpine`
- `compose` — a parsed Docker Compose object (JSON-compatible at this stage)
- `manifest` — an existing Servora manifest for normalization/validation

Risk scanner checks include privileged mode, host networking, host mounts, container-engine sockets, devices, extra capabilities, custom security options, shell commands, missing resource limits, and privileged host ports.

API:

```text
POST /api/ai/import
```

Example request:

```json
{
  "source": {
    "type": "oci_image",
    "image": "nginx:alpine",
    "name": "nginx"
  },
  "approved": false
}
```

If medium/high/critical findings exist, the API returns `approval_required` until the caller explicitly sends `approved: true`. Imported entries are stored as `ai_imported` marketplace entries and are still ordinary Servora manifests; the analyzer is not granted shell access.

### AI Provider (0.1.7)
- Dependency-free OpenAI-compatible provider
- Local/cloud compatible through `SERVORA_AI_BASE_URL`, `SERVORA_AI_MODEL`, `SERVORA_AI_API_KEY`
- `SERVORA_AI_PROVIDER=mock` remains the safe default
- `POST /api/ai/plan` generates a structured container plan; it never grants shell access
- Provider responses are JSON-validated before being returned to the management layer
