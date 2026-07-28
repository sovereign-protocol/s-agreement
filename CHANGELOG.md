# Changelog

## Unreleased

- Agreement agenda items can now be reordered by dragging, like Kanban agenda
  items, and no longer appear as blank document sections.
- Expanded facade API v1 with explicit agreement and agenda commands, removed
  its mutable Session escape hatch, and documented query results as snapshots.
- Application code now uses Session queries and namespaced metadata only.
- Mutation, agenda, and peer-reaction routes now reject nodes outside an
  Agreement topic, including locally absent peer nodes.
- Core retired the direct HTTP channel. `adopt_peer_changes` returned a sync
  effect nothing can deliver any more; it now returns the result alone.
  Sharing an agreement over a relay is unchanged.
- The two-client tests connect over a relay folder instead of an in-process
  HTTP stand-in, the same change S-Kanban and Core made. Behaviour
  unchanged; see Core's `DESIGN_TOPIC_HOME_CHANNELS.md` section 3.
- An agreement can be deleted (`/api/agreement/agreements/delete`). Deleting
  one stops sharing it first, so peers are not left syncing a document this
  side no longer has. Unlike a board there is no last-one guard: nothing
  creates an agreement on demand, and a host with none is a valid state.
- The facade exposes an agreement's `sections` and their `clauses`, so
  Personal Cockpit can show a whole agreement without importing this
  package.
- Moved out of the Core repository into its own, where every other
  application already lives.
- Relicensed from `LGPL-3.0-or-later` to **Apache-2.0**. It carried Core's
  licence only because it sat inside Core's repository, whose `NOTICE` makes
  the repository licence the default for examples. As an application it takes
  the application licence, matching S-Kanban and Personal Cockpit. Sole
  copyright holder, so no contributor consent was required.
- Added a desktop entry point and the `desktop` extra, so it can open in its
  own window like S-Kanban.

Its history from inside the Core repository is preserved; commits before this
point were made while it lived at `examples/s-agreement`.

## 0.1.0a1 — unreleased from the Core repository

- Agreement documents with sections and clauses, reorderable and multi-line.
- Peer-only nodes presented as proposals to accept or withdraw.
- Versioned application facade for cross-application consumers.
