# Aisland Mandates

- **Workspace-Relative Path Resolution:** All service binaries, data directories, and logs MUST resolve to the `aisland/` directory inside the current workspace root — discovered dynamically (e.g., by walking up from the invocation `cwd` to find `TASKS.md` or `.git`), never hardcoded to an absolute path like `~/source/aisland/`. The workspace root is derived from the `cwd` at invocation; the target directory is always `<workspace_root>/aisland/`, regardless of the subdirectory the `aisland` command is run from. Never pollute the workspace root itself with service state.
- **PID-Based Lifecycle:** Process tracking MUST use PID files with `pgrep` pattern fallback. Never assume a service is stopped just because the PID file is missing.
- **Cross-Platform:** Process management (start, stop, signal handling) MUST work on macOS, Linux, and Windows. Use `SIGTERM` on Unix, `taskkill` on Windows.
- **Service Isolation:** Each service (Jenkins, MLflow) MUST have its own data directory, log file, and PID file. Never share state between services.
- **BaseService Contract:** All new services MUST extend `BaseService` and implement `start()`, `stop()`, `status()`, and `reset()`.
- **Job Seeding:** Jenkins `init` MUST auto-create multi-branch pipeline jobs for all workspace projects using config.xml templates.
