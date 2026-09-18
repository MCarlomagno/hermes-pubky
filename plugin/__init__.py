"""Catalog entry point for the managed launcher.

The supervisor installs the runtime provider into its dedicated profile.
Loading this companion in an ordinary Hermes profile only adds setup help.
"""


def _setup_help(raw_args: str) -> str:
    return (
        "Pubky agents run through the managed launcher. In a terminal with "
        "the verified Hermes 0.21.3 environment activated, run:\n\n"
        "  hermes-pubky agent init default\n"
        "  hermes-pubky run default\n\n"
        "For an existing agent:\n"
        "  hermes-pubky agent attach <pubky-uri>\n"
        "  hermes-pubky run <agent-id>\n\n"
        "Hermes installs the pinned launcher dependency with this helper. "
        "If dependency installation was disabled or failed, run:\n"
        "  uv pip install hermes-pubky==0.2.2\n\n"
        "Enabling this companion does not sync the current Hermes profile. "
        "The launcher creates a dedicated profile and syncs its instructions, "
        "memories, skills, conversation database snapshots, portable "
        "configuration, and workspace to your remote Pubky homeserver. "
        "Your homeserver operator can read that data; it is not end-to-end encrypted."
    )


def register(ctx) -> None:
    ctx.register_command(
        "pubky",
        handler=_setup_help,
        description="Show how to create or attach a managed Pubky agent",
    )
