# DeployPilot

DeployPilot is a production-grade Python application for automating Git-based Docker Compose deployments on Synology NAS servers via authenticated GitHub push webhooks.

## Key Features
- **Cryptographic Verification**: Constant-time HMAC-SHA256 signature verification (`X-Hub-Signature-256`).
- **Strict Authorization**: Whitelist-based repository, branch, and commit authorization.
- **Path Traversal Protection**: Enforced deployment root directory checks.
- **Safe Git Operations**: Non-destructive updates with dirty working tree detection.
- **Process Isolation**: Docker Compose execution with process group timeouts and output redaction.
- **Concurrency & Deduplication**: Per-repository locks, bounded concurrency semaphore, and SQLite delivery tracking.
- **Zero-Downtime Health Checks**: Verification of container health status before confirming deployment.
