# Exact-build QA evidence

One directory per released commit: `docs/qa/<commit sha>/` holding the release report of
`scripts/verify_release.sh --with-e2e` (`var/releases/<sha>_<ts>.txt`) and the dashboard, desktop
and browser E2E outputs of that exact commit (spec 31 and 34; runbook section 8).

None exists yet: the waves D1 to D3 are not committed, so no exact-build report could be produced.
The latest measured results on the working tree, with the commands to reproduce them, are in
[../qa_evidence.md](../qa_evidence.md).
