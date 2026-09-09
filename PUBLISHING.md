# Publishing v0.1.0

Everything below is intentionally left for a human: nothing here has been
committed, pushed, tagged or published. The working tree is a complete,
validated repository with `git init` already run and no commits.

## 1. Create the GitHub repository  ✅ done

```bash
gh repo create MCarlomagno/hermes-pubky-memory --public \
  --description "Portable Hermes agent context over Pubky" \
  --homepage "https://github.com/MCarlomagno/hermes-pubky-memory"
```

## 2. Commit and push  ✅ done

```bash
cd ~/repos/hermes-pubky
git add -A
git commit -m "feat: portable Hermes agent context over Pubky (v0.1.0)"
git branch -M main
git remote add origin git@github.com:MCarlomagno/hermes-pubky-memory.git
git push -u origin main
```

CI runs on that push: Rust lint + unit tests, the e2e suite against a real
Pubky v0.11 testnet (with a Postgres service container), the Python suite on
3.11/3.12/3.13, and the Hermes integration check. Let it go green before
tagging.

## 3. Configure PyPI trusted publishing  ✅ done

No API token is stored in the repository — the release workflow authenticates
with OIDC. Create the publisher **before** tagging:

1. Go to <https://pypi.org/manage/account/publishing/>.
2. Add a new pending publisher. These must match exactly — the repository is
   named `hermes-pubky-memory` while the PyPI package is `hermes-pubky`, and a
   mismatch here fails the release with an OIDC error:
   - PyPI project name: `hermes-pubky`
   - Owner: `MCarlomagno`
   - Repository name: `hermes-pubky-memory`
   - Workflow name: `release.yml`
   - Environment name: `pypi`
3. The `pypi` environment does **not** need to exist first — Actions creates
   it on first use, and the OIDC token carries the name either way. Create it
   yourself (Settings → Environments → New environment, named `pypi`) only if
   you want a required reviewer as a manual gate before each publish.

## 4. Tag the release  ← you are here

```bash
git tag -a v0.1.0 -m "hermes-pubky v0.1.0"
git push origin v0.1.0
```

The `Release` workflow then:

1. builds a macOS `universal2` wheel (arm64 + x86_64 in one binary) and
   manylinux2014 wheels for x86_64 and aarch64 — abi3, so each one covers
   Python 3.11–3.13,
2. smoke-tests every wheel with `scripts/smoke_test.py`,
3. builds an sdist,
4. publishes to PyPI via trusted publishing,
5. creates the GitHub release with the wheels, the sdist and a `SHA256SUMS`
   file attached.

## 5. Verify the published package

```bash
uv venv --python 3.11 /tmp/verify && \
uv pip install --python /tmp/verify/bin/python hermes-pubky==0.1.0 && \
/tmp/verify/bin/python -c "import hermes_pubky; print(hermes_pubky.__version__)"
```

## Not done, deliberately

- **Hermes community-index submission** — deferred per plan until the public
  index is operational and its native-dependency install story fits a wheel
  with a compiled extension.
- **Windows wheels** — out of scope for v0.1.
- **Publishing public contexts** — v0.1 reads them only.

## Note on macOS wheels

GitHub retired `macos-13`, the last x86_64 macOS runner image; only arm64
images (`macos-14/15/26`) are published now. A job targeting `macos-13` does
not fail — it queues indefinitely. Intel macOS wheels are therefore built by
cross-compiling a `universal2` binary from an arm64 runner, which has the
added benefit that the smoke test actually executes the artifact we ship
(the runner loads the arm64 slice).

Every job now carries `timeout-minutes`, so a runner that can never be
allocated fails in under an hour instead of hanging for six.
