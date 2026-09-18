# Hermes fixtures

`hermes_0_21_3_schema30.json` records synthetic conversations created by
`scripts/generate_hermes_fixture.py` against Hermes commit
`a51143fbbe6ddbc0c7f403d0579c4d75504c6793`. Generate it in the verified runtime
with `python scripts/generate_hermes_fixture.py`.

`hermes_0_19_0_schema22.json` is the original recorded contract.
`hermes_0_19_0_schema22.sqlite3.gz` is an actual database from Hermes 0.19.0,
created with the fixture generator at this repository's commit
`e4d5ec29e99064758bbc744150d272ec84b29d42` using `--keep`. The plain session
(`aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa`) additionally has `system_prompt` set to
`Legacy synthetic system prompt.` so migration exercises prompt deduplication.
The closed database was compressed with Python's `gzip.compress(..., mtime=0)`.

All identities, paths, messages, and configuration values in these fixtures are
synthetic. Never regenerate them from a real Hermes profile. Keep the legacy
database unchanged: the upgrade tests need the old schema, including its search
indexes, rather than a reconstructed approximation of it.
