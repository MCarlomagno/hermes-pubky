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

## 3. Configure PyPI trusted publishing  ← you are here

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

## 4. Tag the release

```bash
git tag -a v0.1.0 -m "hermes-pubky v0.1.0"
git push origin v0.1.0
```

The `Release` workflow then:

1. builds wheels on native runners for macOS arm64, macOS x86_64,
   manylinux2014 x86_64 and manylinux2014 aarch64 (one abi3 wheel each,
   covering Python 3.11–3.13),
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
