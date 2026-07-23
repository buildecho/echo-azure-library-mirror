# Tests

The tests for `library-mirror` live here. Both suites run locally without any credentials or cloud accounts, so you can check your changes before opening a pull request.

## Unit tests

`test_sync.py` covers the core logic: parsing library specs, extracting and sorting versions (PEP 503 and PEP 440), redacting credentials from logs, and validating configuration. They run fast and need no network.

```bash
pip install -r library-mirror/requirements.txt pytest
pytest test/ -q
```

## End-to-end test

`local-e2e.sh` runs a full mirror against local stand-ins for Echo and Azure Artifacts, so the whole pull-and-publish path runs on your machine:

- **npm** uses two [Verdaccio](https://verdaccio.org/) registries. One stands in for Echo and proxies the public npm registry, so any public library is available; the other is an empty feed that receives what the mirror publishes.
- **PyPI** uses two [pypiserver](https://github.com/pypiserver/pypiserver) instances. One stands in for Echo, seeded with a real library; the other receives the uploads.

The test confirms that libraries arrive at the destination, that each artifact is byte-for-byte identical to its source, and that a second run skips everything already published.

```bash
test/local-e2e.sh      # requires docker, python3, and node/npm
```

## What the local tests do not cover

Echo's security check runs inside Echo's registry and cannot be reproduced locally, and the local stand-ins never actually invoke the action itself — they call its underlying script directly. The local tests cover everything up to that point; the real Azure DevOps test below covers the rest.

## Real Azure DevOps test

Three scripts, driven by a CI job rather than a single local command, since the middle step runs the action itself the way any caller would (`uses: ./library-mirror`), not a stand-in for it:

1. `azure-e2e-setup.sh` creates a throwaway feed in a real Azure DevOps org/project via the Feeds REST API, and hands back its registry URLs.
2. The action runs against that feed as a normal workflow step, mirroring a fixed library list from a real Echo Libraries key.
3. `azure-e2e-verify.sh` downloads those libraries from the feed with the real npm/pip clients and confirms they're there.
4. `azure-e2e-teardown.sh` deletes the feed — always, whether the run passed or failed.

Runs in CI as the `azure-e2e` job. Needs `AZURE_DEVOPS_ORG` / `AZURE_DEVOPS_PROJECT` repo variables, and `AZURE_TOKEN` (scoped to Packaging **Read, write, & manage** — broader than the Read & write scope end users need, since this also creates/deletes the feed) / `ECHO_TOKEN` repo secrets.

## Real Azure DevOps upstream-fallback test

`azure-e2e-upstream.sh` covers the other half of the consumer story: a library that was **never** mirrored should still resolve, because the feed proxies it from its public npm / PyPI upstream. It installs a package that isn't synced anywhere (`left-pad` for npm, `requests` for PyPI) with the real npm/pip clients and asserts it resolves.

It uses a **separate** feed from the sync test on purpose: a feed with a same-ecosystem public upstream makes every public version look "already present" to the mirror's idempotency check, so sharing one feed would let the upstream shadow the sync and quietly weaken the sync test. Keeping them apart keeps each test honest.

Unlike the sync test, this one does **not** create its feed. A public upstream that actually resolves packages can only be created through the Azure DevOps **portal** ("Include packages from common public sources") — an upstream added via the Feeds REST API reports status `ok` but never serves upstream packages (confirmed repeatedly in testing). So the feed is a one-time, hand-created, reusable fixture, and the test only consumes from it.

**One-time setup:** in the `AZURE_DEVOPS_PROJECT`, create a feed via the portal with "Include packages from common public sources" ticked (npm + PyPI), then set the `AZURE_UPSTREAM_FEED` repo variable to its name.

Runs as the final check in the `azure-e2e` job (it needs no feed of its own to create, so it shares that job's runner rather than spinning up another). It uses the `AZURE_DEVOPS_ORG` / `AZURE_DEVOPS_PROJECT` / `AZURE_UPSTREAM_FEED` repo variables and the `AZURE_TOKEN` secret (no Echo key — this path never touches Echo), and runs even if the sync steps above fail so its result is always reported.
