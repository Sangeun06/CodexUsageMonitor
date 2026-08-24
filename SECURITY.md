# Security

This project builds two kinds of collector packages:

- `generic` packages contain no collector token.
- `provisioned` packages and the Windows machine installer contain the central collector token.

Provisioned artifacts are credentials. Keep the repository private, restrict access to Releases, and never attach them to a public issue or repository. If an artifact is exposed, replace `data/collector.token`, update the GitHub `COLLECTOR_TOKEN` secret, rebuild the packages, and redeploy every collector.

The dashboard password, collector token, monitor database, local build output, Codex authentication files, and session databases must never be committed. The repository `.gitignore` excludes the local `data/` and `dist/` directories.
