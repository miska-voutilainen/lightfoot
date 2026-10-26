# Security

Lightfoot sits between your coding agent and the model API and sees everything both send. This page says what it guarantees and how to report a problem.

---

## Reporting a vulnerability

Don't open a public issue. Use GitHub's private vulnerability reporting: the repository's **Security** tab, then **Report a vulnerability**. Include the version (`lightfoot --version`), your OS and the steps to reproduce. You'll get a reply within a week.

---

## Guarantees

| Area | Guarantee |
| --- | --- |
| Network | Listens on 127.0.0.1 only. Requests with a foreign `Host` header (DNS rebinding) or any `Origin` header (web pages) are refused, on every route. |
| Control | Shutting the proxy down needs a custom header that a browser can't send across sites without a preflight. |
| Credentials | Auth headers are forwarded upstream unchanged and never written to logs, the database, or spill and history files. |
| Projects | A repository's `.lightfoot/config.toml` can't change where requests go (`port`, the upstream URLs) or set a command or folder Lightfoot uses (`handoff_command`, `vault_path`, `handoff_folder`). |
| Data | Nothing is sent anywhere except the requests your agent already makes. There's no telemetry. |
| Storage | Stored tool output and condensed history stay on your machine, in folders only your user can read (`0700` on macOS and Linux), and are deleted after the retention period. |
| Limits | Request bodies over 100 MB are rejected before they are read in full. |

---

## In scope

- Anything that breaks one of the guarantees above.
- A way to make Lightfoot forward a modified request that its validator should have rejected.
- A way for another local user or a web page to read or change Lightfoot's data or traffic.
- A way for a cloned repository to make Lightfoot send requests elsewhere or run code.
