# Exact-build QA evidence

One directory per released commit: `docs/qa/<commit sha>/` holding the release report of
`scripts/verify_release.sh --with-e2e` (`var/releases/<sha>_<ts>.txt`) and the dashboard, desktop
and browser E2E outputs of that exact commit (spec 31 and 34; runbook section 8).

Reports:

- [cee1a15aaa87583d8e2847bb14253932273855d4](cee1a15aaa87583d8e2847bb14253932273855d4/README.md):
  `verify_release.sh --with-e2e`, clean tree, result **verified** (2026-10-10).

The commands to reproduce every check are in [../qa_evidence.md](../qa_evidence.md).
