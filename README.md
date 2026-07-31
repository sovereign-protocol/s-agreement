# S-Agreement

S-Agreement is a local-first application for documents people have to agree to:
working agreements, terms, policies. It is built on Sovereign Core, so every
participant keeps an explicit local perspective and differences stay visible
rather than being silently overwritten by a central copy.

Where a Kanban board merges a peer's change and lets you react afterwards, an
agreement does the opposite. A peer's node with no local counterpart arrives as
a **proposal** — accept it or withdraw it. A document is a thing people agree to
before it is true, so the adoption step is the point rather than an obstacle.

## Quickstart

Requires Python 3.10+ and Sovereign Core `>=0.1.1,<0.2`.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\sovereign-host.exe 9307 config/agreement.example.json
```

Open <http://127.0.0.1:9307>. Direct HTTP is intended for LAN/VPN use. Local
folder and SFTP mailbox channels are configured through relay targets.

## Organizations

Agreements can contain subagreements, forming an organizational tree. Each
subagreement is still an independent shared topic:

- the parent participants explicitly accept the organizational link;
- the subagreement participants separately accept their invitation; and
- joining a parent never grants access to a child;
- every ancestor must have a current, unexpired acceptance before a child
  invitation can be mounted.

This permits a broad organization agreement with smaller circles, teams, or
roles beneath it, each shared only with its own participants. The organization
panel shows the agreements available locally and marks child agreements that
have separate membership.

Each participant's acceptance or refusal is stored as its own agreement node.
It records the decision time, optional expiration (infinite by default), and a
SHA-256 reference to the exact agreement content. A content change makes older
acceptances visibly outdated until that participant renews them.

## Desktop window

The same host can draw into its own window instead of a browser tab:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[desktop]"
.\.venv\Scripts\s-agreement-desktop.exe
```

The window picks a free port at start-up, so documents are kept in a per-user
directory (`%LOCALAPPDATA%\S-Agreement` on Windows) rather than beside the port
number. Pass a config file to override anything, including `storage_file`.

## History

S-Agreement began inside the Core repository as its conformance example —
the worked application proving Core's contract could be implemented by
something other than S-Kanban. It moved here once it became a product in its
own right. Commits before that point were made at `examples/s-agreement`.

## License

Application software and assets are Apache-2.0, as every Sovereign application
is. Documentation is CC-BY-4.0. Sovereign Core is a separately replaceable
LGPL-3.0-or-later dependency.
