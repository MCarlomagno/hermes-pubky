# Portable agents

A user's agent belongs to the user and remains available across the computers that run it. Public templates provide reusable starting points, while personal context belongs to the user.

## Language

**Managed agent**:
A user's persistent assistant, comprising its instructions, memories, skills, portable preferences, saved conversations, and workspace. The same managed agent can be used on successive computers.
_Avoid_: Memory overlay, running process

**Homeserver**:
The user's authoritative store for the agent's saved information and assets, accessible independently of the computer running the agent.
_Avoid_: Memory backup, secondary copy

**Harness**:
The software that runs the agent using its saved information, a model, and available tools. Hermes is the first harness supported by this project.
_Avoid_: Agent storage, homeserver runtime

**Public template**:
A publicly addressable collection of reusable agent instructions and assets that another user can adopt. It excludes the adopting user's personal information.
_Avoid_: Public personal agent, shared private context

**Agent memory**:
The personal facts and learned notes the agent retains for future conversations. Agent memory is one part of a managed agent's saved state.
_Avoid_: Server RAM, entire conversation archive

**Managed workspace**:
The collection of documents, references, and outputs included in a managed agent's portability boundary. Files elsewhere on a computer are not implicitly members of it.
_Avoid_: Entire computer, execution sandbox

**Checkpoint**:
A recoverable saved version of a managed agent's included state. It is confirmed portable only after it has been saved successfully to the homeserver.
_Avoid_: Running session, pending local edit

**Working copy**:
The local state used by a harness to run a managed agent. A working copy may contain changes that do not yet belong to a confirmed checkpoint.
_Avoid_: Always-disposable cache, independent canonical agent

**Handoff**:
Moving use of a managed agent from one computer to another through a confirmed checkpoint. A handoff carries saved work, not ownership of already running processes.
_Avoid_: Live process migration, simultaneous multi-device editing

## Example dialogue

Developer: “Does moving to another computer mean starting with an empty agent?”

User: “No. The harness should recover my saved agent information from my homeserver.”

Developer: “What can someone else reuse?”

User: “The public template. My personal information belongs to my own agent.”

Developer: “Can I discard this working copy now?”

User: “Only after its changes are in a confirmed checkpoint. Then I can hand off the managed agent and recover its workspace on another computer.”
