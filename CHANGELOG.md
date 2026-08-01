# Changelog

## 0.2.0a3 - 2026-08-01

Renamed from S-Agreement to **S-Team**, distributed as `sovereign-team`. The
application id is now `team`, routes are served under `/api/team/`, and the
Python package is `s_team`. The `agreement_*` node types are unchanged: a team
is constituted by an agreement, so that word names the document rather than
the application, and nothing about the stored tree moves.

Roles, actors, and the organization built out of them, following
`DESIGN_ROLES_AND_ACTORS.md`. Taking part in an agreement is now holding a
role in it, and an actor is a person *or an agreement* — which is what makes
the organizational tree fall out of the role model rather than sit beside it.

- Add `agreement_role` as document content, carrying `agreement_accountability`
  and `agreement_domain` items as nodes rather than lists, so two people
  editing different accountabilities diverge separately as clauses do.
- Hold a role through two records with two authors: an `agreement_role_offer`
  written by the agreement's Identity holder and an `agreement_role_decision`
  written by the actor, carrying the UTC decision time, an optional expiration,
  and a SHA-256 reference to what was accepted. A role is live only while both
  stand, so either side can end it without deleting what the other wrote — and
  each half means something alone: an offer with no answer is an unfilled seat,
  an answer with no offer is somebody asking for the role. That is how a
  newcomer, who can be offered nothing until they hold something, gets their
  first role.
- Mark a revoked offer instead of deleting it, so the actor's surviving answer
  does not read as a fresh request; offering again revives the same record.
- Keep one decision per actor per role, rewritten rather than added to.
  Reconsidering a refusal otherwise leaves two records for one actor, with
  which of them counts decided by iteration order.
- Scope acceptance to the document body plus that role's own definition, so
  editing one role no longer re-opens everybody else's acceptance while
  editing the document still re-opens everyone's.
- Give each agreement an `agreement_identity`: one node naming the holder,
  singular because everybody writes the holder into the same node, so the
  protocol's own convergence carries the consent. Handover is a proposal the
  new holder adopts, and a contested claim settles through the existing
  adopt/rollback buttons rather than through machinery of its own. Holding
  Identity is holding a role, and counts as being part of the agreement.
- Read the read-only guard from role holdings and nothing else. Invalidity is
  derived rather than written, so leaving a parent closes its descendants
  without destroying the answers held there, and taking the parent back up
  restores the whole subtree at once.
- Add consent-based organizational trees. A subagreement is an Agreement actor
  holding an ordinary role in its parent, plus an `agreement_role_holding` in
  the child naming that seat; both sides must name it before the relationship
  exists anywhere. Every subagreement stays a separately invited topic, so
  joining a parent never grants access to a child, and a subagreement
  invitation mounts only once every ancestor has a current, unexpired
  acceptance. Adding a subunit re-opens nobody's acceptance, because a seat is
  a role and roles are outside the document body hash.
- Let an agreement hold seats in several parents. An agreement is writable when
  *any* of its seats reaches a root with every step of that path live, so one
  parent lapsing suspends that relationship rather than paralysing a body
  another still carries. Home — where the organization draws it — is derived,
  never stored: the first seat in order whose path validates, which makes the
  order of the seats the whole of the control. Seating refuses a seat that
  would close a loop, best-effort per replica.
- Answer for an agreement through whoever holds its Identity, recorded in
  `decided_by`, since an agreement cannot answer for itself and has no replica
  its answer could be read from. Seats offered to an agreement are surfaced on
  it, where the authority to answer them lives, and can be declined or given
  up.
- Make a template an agreement with nobody in it — no type, no flag, no mode.
  Copying takes the text, the roles and their definitions, and none of the
  taking part; uuids are fresh throughout, because an acceptance is keyed by
  role uuid. State is read back off the actors: template, instantiated, or
  working.
- Show an organization panel with the local hierarchy, its topic-scoped
  membership, restricted placeholders for children shared with somebody else,
  and an "also in" marker on any row whose other seats home leaves out.
- Show avatar-and-name badges for where each holder stands: accepted, refused,
  pending, expired, outdated, requested, revoked, and unobserved — the last
  being "I cannot see whether they answered", which is a different fact from
  "they have not". A person's badge and a body's badge draw differently.
- Rework the page around the model: your own row first with your badges as the
  control, role cards as definitions and invitations with the Identity
  holder's controls on them, subagreements as Agreement holders on the role
  that seats them, and the seats an agreement holds as badges on its own line.
- Preserve focused inputs during periodic refreshes, and skip the rebuild
  entirely when the payload has not changed.
- Cut payload builds from 1125 ms to 67 ms. `Session.identity` was read 349
  times per build and each read snapshots the whole protocol tree; reads now
  memoise inside a scope, and nothing outside a read caches, so a mutation can
  never be served a stale entry.
- Deleting a parent promotes its child agreements rather than cascading:
  their side of the seat goes and they become roots. Deleting a seated
  agreement empties only its own seat — the answer written on its behalf —
  leaving the parent's role, its offer, and every other actor holding that
  role untouched.
- Stepping out of a seat clears both sides of it, so the parent stops
  reporting a seat the agreement no longer claims, and leaves the offer
  standing so it can be answered again. Stepping out of an agreement no peer
  has yet no longer reports a failure for work it had done.
- Expand facade API v1 with the identity, role, seat, organization and
  template queries and commands, and advance the data schema to version 3.
  `subagreement_links`, `acceptance_badges` and `set_decision` are gone, for
  `child_agreements`, `participants` and `decide_role`.
- Development builds between releases carried `agreement_link`,
  `parent_agreement_uuid` and `agreement_decision`. All three are retired;
  none of them shipped in a release, so only a repository checkout is
  affected.

## 0.1.0a2 - 2026-07-30

- Require Sovereign Core 0.1.5 for composite application responses.
- `/api/team/document` now uses Core's atomic
  snapshot-observe-merge boundary, so a relay poll cannot tear one response.
- Agreement selection metadata is now read as a snapshot and written in
  one Session transaction, matching Core 0.1.5's locked metadata contract.
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
  HTTP stand-in, the same change S-Initiative and Core made. Behaviour
  unchanged; see Core's `DESIGN_TOPIC_HOME_CHANNELS.md` section 3.
- An agreement can be deleted (`/api/team/agreements/delete`). Deleting
  one stops sharing it first, so peers are not left syncing a document this
  side no longer has. Unlike a board there is no last-one guard: nothing
  creates an agreement on demand, and a host with none is a valid state.
- The facade exposes an agreement's `sections` and their `clauses`, so
  S-Cockpit can show a whole agreement without importing this
  package.
- Moved out of the Core repository into its own, where every other
  application already lives.
- Relicensed from `LGPL-3.0-or-later` to **Apache-2.0**. It carried Core's
  licence only because it sat inside Core's repository, whose `NOTICE` makes
  the repository licence the default for examples. As an application it takes
  the application licence, matching S-Initiative and S-Cockpit. Sole
  copyright holder, so no contributor consent was required.
- Added a desktop entry point and the `desktop` extra, so it can open in its
  own window like S-Initiative.

Its history from inside the Core repository is preserved; commits before this
point were made while it lived at `examples/s-team`.

## 0.1.0a1 — unreleased from the Core repository

- Agreement documents with sections and clauses, reorderable and multi-line.
- Peer-only nodes presented as proposals to accept or withdraw.
- Versioned application facade for cross-application consumers.
