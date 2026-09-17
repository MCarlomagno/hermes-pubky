# Start a Pubky agent

Requires Python 3.11–3.13, **Hermes 0.19.0 exactly**, and a Pubky homeserver
on v0.11 or later. Use a separate Hermes 0.19.0 environment if your current
Hermes version differs.

Newer Hermes installers read this plugin's pinned Python dependency. Hermes
0.19.0 only copies the plugin directory, so first install the launcher in
your activated Hermes 0.19.0 environment:

```sh
uv pip install hermes-pubky==0.2.0
```

The package includes the native Pubky extension. Release wheels support macOS
and Linux; building from source requires Rust.

Then create and run an agent:

```sh
hermes-pubky agent init default
hermes-pubky run default
```

To attach an existing agent, use `hermes-pubky agent attach <pubky-uri>`,
then `hermes-pubky run <agent-id>`. Initialization asks Pubky Ring to authorize
access to the agent's directory.

The companion adds `/pubky` setup help when enabled. It does not turn your
current Hermes profile into a synced agent. Always start managed agents with
`hermes-pubky run`; the launcher prepares their dedicated profiles and installs
the runtime provider there.

[Full documentation](https://github.com/MCarlomagno/hermes-pubky#readme)
