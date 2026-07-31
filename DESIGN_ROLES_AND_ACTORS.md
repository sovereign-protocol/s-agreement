# Roles and Actors — design and implementation plan

Roles turn an agreement from a document people accept into a structure people
take part in. This doc records the model settled in design discussion, the
rules that follow from it, and the order the work is built in.

Status tags follow the Core convention: **[DONE]** built and tested,
**[PROPOSED]** decided here but not built, **[OPEN]** unresolved.

Everything below is **[PROPOSED]** unless marked otherwise.

## 1. Model

### 1.1 Actor

An Actor is anything that can hold a role. Actor is `{Individual, Agreement}`.

| Kind | Reference | Stability |
|---|---|---|
| Individual | identity node uuid | Stable per person. Siblings share one profile subtree — `adopt_pairing_identity` preserves `incoming.uuid` (session.py), so a person's clients are one Actor. |
| Agreement | agreement node uuid | Stable, shared across replicas. |

`account_key` (`DESIGN_MULTI_CLIENT_IDENTITY.md`) is **not** a prerequisite.
That doc is the road not taken; `DESIGN_MULTI_CLIENT_PAIRING.md` is **[DONE]**
and already gives one identity uuid per person.

**To become part of an agreement, an Actor takes a role.** There is no
membership independent of role-holding.

### 1.2 Node schema

```
agreement                          (topic, unchanged)
├── agreement_section              (unchanged)
│   └── agreement_clause           (unchanged)
├── agreement_identity             singular; data.holder_actor_uuid
├── agreement_role                 data.name, data.purpose, data.order
│   ├── agreement_accountability   data.text, data.order
│   ├── agreement_domain           data.text, data.order
│   ├── agreement_role_offer       one per (role, actor); authored by Identity
│   └── agreement_role_decision    one per (role, actor); by the actor
└── agreement_role_holding         this agreement's seat elsewhere:
                                   data.parent_agreement_uuid, data.role_uuid,
                                   data.order — the ordered parent list
```

Three levels for holdings, not two, because a role may have several holders
and revoking one must not revoke the others. Offers are per-(role, actor).

Accountabilities and domains are **nodes, not lists in `data`**. Same reason
clauses are nodes: `REACTABLE` is per-node, and a JSON list collapses two
people editing different accountabilities into one undiffable divergence.

`agreement_link` is retired — a sub-agreement is an Agreement actor holding a
role in the parent. `agreement_decision` is retired in favour of
`agreement_role_decision` on the default Participant role.

### 1.3 Cardinality

- A role has 0..n holders. A **vacant role is not a problem** — it is defined
  work nobody has taken.
- **Identity has exactly one holder.** It is shaped differently from other
  roles because of cardinality, not privilege (§2.2).

**[DONE]** Because Identity *is* a role, holding it is being part of the
agreement, and `_has_current_acceptance` counts it. Anything else lets the
application tell the person who speaks for an agreement to take a role in
it before they may act — which is how this was found. The consequence is
that somebody cannot step out of their own agreement by refusing roles
while still holding Identity; they have to hand Identity on first.

## 2. Rules

### 2.1 Authority

The Identity holder is the only actor whose role offers others adopt.

This is a **coordination rule, not a security boundary.** Core has no content
signing — every crypto path is SFTP transport key material, `identity_key` is
a bare `uuid4()`, and `revision_origin` is authorship bookkeeping. A peer can
write a node carrying another identity's origin and nothing downstream can
tell. This is already true of every node in the protocol; roles are only the
first content where forged data would confer authority.

Accepted deliberately: this software supports sovereign people who have
chosen to sync with each other. Recorded here so nobody later mistakes it for
a guarantee. Making it a boundary means keypairs in Core, not work here.

It applies in two independent layers:

- **Affordance** — the client does not offer Identity actions to someone who
  does not hold Identity from their own perspective. Prevents accidents.
- **Adoption predicate** — a proposed offer node is not adopted unless its
  author holds Identity in the observed state. Prevents intent.

Plus one condition: **if the Identity node is diverged in my view, adopt no
internal offers and surface it.**

### 2.2 Identity is convergence, not protocol

`agreement_identity` is a single node per agreement carrying the holder. It
needs no offer/accept protocol on top, because the protocol's own semantics
already express consent:

| Situation | My replica | Their replica | Event |
|---|---|---|---|
| Alice holds it | Alice | Alice | `in_agreement` |
| Alice offers to Bob | Bob | Alice | `peer_made_changes` |
| Bob accepts | Bob | Bob | `in_agreement` |
| Bob self-installs, Alice idle | Alice | Bob | `peer_made_changes` |
| Both write at once | Charlie | Bob | `divergence` |

**[DONE]** The event is `divergence` only when *both* sides wrote since
their last common state. One side writing while the other sits still is an
ordinary peer change — measured, not assumed: `test_identity_handover_
converges_and_a_claim_diverges` and `test_two_sides_naming_different_holders_
at_once_diverge`.

That distinction changes nothing structurally, because both event types carry
`peer_addr` and both are settled by the same `accept_peer_node` /
`rollback_peer_node`. It does change wording: a self-install cannot be
promised to "create a divergence", so the interface warns about a competing
record the other side must settle, which is true in both cases. The view
keys off the holder each replica names, never off the event type.

Handover is not a new verb: Alice writing `holder = Bob` is a proposal, Bob
accepting it is `accept_peer_node`. A contested claim is the same shape and
the response is `accept_peer_node` or `rollback_peer_node`. Both already
exist. `divergence` is the top-priority transition event
(`TRANSITION_PRIORITY` = 6, session.py).

This deletes three things that would otherwise need building: a vacancy rule,
a claim-legality predicate, and ambiguity-freeze machinery.

**Ordinary roles cannot use this encoding**, because absence conflates *has
not seen it yet* with *said no*. Refusal is a first-class act in a
consent-based system, so ordinary roles keep an explicit decision node — which
also carries expiry and the reference hash.

**Expired Identity is not vacant.** The holder is expected to hand over to a
successor; expiry marks the norm, it does not release the seat. Combined with
no automatic vacancy, Identity becomes vacant only by explicit resignation or
in a template, which is why one mechanism (warned self-install resolved by
divergence) is enough.

### 2.3 Withdrawal — one principle, two verbs

> **You may withdraw what you authored. You may never delete what someone
> else wrote.**

- Identity authored the offer → Identity may withdraw it. That is
  **revocation**.
- The actor authored the decision → the actor may delete it. That is
  **resignation**.

**[DONE]** Revocation *marks* the offer `revoked_at` rather than deleting it.
Deleting was the original design and is wrong: with §2.5's request mechanism,
an answer with no offer beside it means somebody asking for the role, so a
deleted offer would leave the actor's surviving answer reading as a fresh
request — and the Identity holder would immediately be prompted to re-offer
exactly what they had just taken back, making revocation useless. Marking is
still a withdrawal of what Identity itself wrote, so the rule holds; it just
keeps the fact that an offer existed. Offering again revives the same record.
A withdrawn offer with no answer left beside it is not shown at all.

This was caught in the running application, not by the tests: the first fix
recorded on the *actor's* node whether it was answering an offer, which is
correct at the moment it is written and wrong forever after, because
confirmation changes the situation and cannot rewrite somebody else's node.

A holding is live only when both nodes are present, so neither party needs
the other's consent and neither can rewrite the other's record. Mechanically
free: deletions propagate as absences, and `adopt_absence` /
`rollback_absence` are already parameters of `accept_peer_node` /
`rollback_peer_node`.

**[DONE]** This is why offer and decision are **siblings under the role, not
nested**. Deleting a container prunes its descendants rather than tombstoning
them — measured in step 1 on a role's accountabilities — so nesting the
decision inside the offer would make revocation delete the actor's own
record, which is precisely what this rule forbids. Both are keyed on
`actor_uuid` under the role instead, leaving each author's node independent.

**[DONE]** Answering an offer adopts it first. This application never merges
a peer's new node automatically — it presents it as a proposal — so an offer
reaches the person it names as a proposal, not as state. `decide_role`
therefore adopts the offer before recording the answer, keeping it one
gesture for the person while still passing through `accept_peer_node`, so the
authority check in §2.1 is not bypassed.

The leftover is harmless. After revocation the actor's decision node survives
pointing at nothing, and is inert. No cleanup pass.

### 2.4 Validity — ANY path, per holding

An agreement is writable when **at least one** holding chain reaches a root
with every link valid. Roots — agreements holding no role anywhere — are
self-standing.

Invalidation therefore suspends *a relationship*, not an entity. Under ALL,
one parent could unilaterally paralyse a body other parents also depend on,
which inverts the sovereignty framing.

Consequence to accept explicitly: *"invalidating a parent invalidates all
children"* becomes **conditionally** true. It holds for single-parent
children — all of them today — and stops holding when a child takes a second
parent.

Validity is **derived, never written**. This deletes the descendant-refusal
cascade in `set_decision` (logic.py), which today overwrites your own
acceptance on every descendant and never un-cascades when the parent is
re-accepted — a latent bug independent of this work.

A path counts as valid only if every agreement on it is joined locally, which
is already what the current guard means by *"Read-only until every parent
agreement is joined."* No separate clause needed.

### 2.4b A request is a decision with no offer

**[DONE]** Only Identity may offer (§2.1), so somebody who has just accepted
a topic invitation holds nothing and cannot be let in by anyone else. Asking
is the move available to them; confirming is the move available to Identity.

This needs **no new node type**. A holding is live only while both records
exist, so the two halves already mean something on their own:

| Offer | Decision | Meaning |
|---|---|---|
| yes | no | an unfilled seat — *pending* |
| no | yes | somebody asking — *requested* |
| yes | yes | held |
| revoked | yes | withdrawn — *revoked* (§2.3) |

Confirming a request is an ordinary `offer_role`. The asker's answer is
already on file, so the holding goes live the moment both records exist and
the newcomer is never asked to answer twice. Neither side writes the other's
record at any point, so §2.3 is untouched.

### 2.5 Home — derived, not declared

Holdings are an **ordered list**. Home is the **first holding in order whose
chain validates**. That is a pure function of (order, validity):

- no `home` field, no declaration step, no stale-home migration
- reverses itself automatically when the original parent recovers
- home edges ⊆ holding edges, one per agreement, over a DAG ⇒ the projection
  is a spanning forest for free, needing no separate cycle check
- an agreement with no valid holding has no home and renders as a local root

Ordering reuses the existing `order` convention read by `_ordered()` and
written by `session.move_child_to_index` — the same primitive behind
`move_section` and `move_clause`. Later ordering policy (activity level, etc.)
is a reorder over the same list and needs no new design.

**Home is display and navigation only. It must never enter the validity
guard** — if "valid" ever means "valid via home", ANY has silently become
ALL-through-one-path.

**Non-home holdings stay visible** as annotations on the agreement's own page,
never hidden. A hidden second parent is a trap for whoever deletes the first.

### 2.6 Peers and actors are two populations

- **Peers** — who you sync this topic with. Transport fact.
- **Actors** — who has accepted a role. Governance fact.

Neither contains the other: someone invited but holding no role is an
observer; someone holding a role you no longer sync with is a member you
cannot see. `acceptance_badges` iterates **actors** and uses **peers** as the
evidence channel.

Acceptance is credible only when read from the actor's own replica — the
current implementation is already right about this and stays.

That adds a status. Today: `{pending, refused, expired, outdated, accepted}`.
Add **`unobserved`** — you know the offer exists but do not sync with the
actor, so you cannot know their answer. It *is* distinguishable from
`pending`, because you know your own peer set. Collapsing them would be a lie
the UI tells.

Consequence worth stating: **an agreement can only be as large as the group
that fully syncs on it.** Sub-agreements are not only a governance device,
they are the replication scaling mechanism — the load-bearing reason the
structure is recursive rather than one large membership list.

### 2.7 Acceptance scope

The reference hash covers **the document body plus the definitions of the
roles this actor holds** — not the whole agreement.

Whole-document hashing (today's behaviour) means editing the Treasurer's
accountabilities re-opens the CFO's acceptance and every sub-agreement's.
Under the scoped hash: editing an unrelated role touches nobody, editing the
agreement text correctly stales everyone, adding a new role stales nobody.

Validity is `min(offer validity, decision validity)`. The offer may bound the
seat ("until Dec 31"); the decision may bound the commitment ("until Sep 30").
Both are meaningful and different.

### 2.8 Templates are a state, not a type

| Actors | State |
|---|---|
| 0 | Template |
| 1 | Instantiated template |
| ≥2 | Working agreement |

No flag, no separate node type, no clone-and-strip mode. Cloning is "copy
structure with fresh uuids, zero decisions", which lands at 0 actors by
construction. Role uuids must be regenerated too — acceptance lookup is
uuid-keyed.

A 0-actor agreement is **inert**: no actors means no Identity, so it cannot
accept, resign, or act. Its holdings can only be removed from the parent side,
by ordinary revocation (§2.3). That is a derived property, not a rule.

Taking Identity in a 0-actor template is the same write as anywhere else,
with nobody to diverge against.

### 2.9 DAG enforcement

Cycles among agreement-actors are rejected at the application layer, using the
existing `creates_cycle` walk generalised to multiple parents.

Enforcement is **best-effort per replica**: you can only detect a cycle among
agreements you have joined. A cycle may exist globally that no single peer
sees.

## 3. Staging

Each step ships a working application. The risky graph rewrite is last, after
roles are proven as content.

### Step 0 — Baseline **[DONE]**

Commit the in-flight tree/sub-agreement/badge work (1,784 insertions across
`logic.py`, `agreement.html`, `agreement.css`, controller, facade, tests) on
its own branch before anything is layered on it.

### Step 1 — Roles as content **[DONE]**

Roles, accountabilities and domains as document nodes. CRUD, ordering,
reactions. No offers, no holdings, no Identity. Purely additive — the existing
acceptance model keeps working, and describing roles is useful on its own.

| Area | Work |
|---|---|
| `logic.py` | `agreement_role` / `agreement_accountability` / `agreement_domain` node types; create/rename/delete/move for each; add all three to `REACTABLE` and `OWNED_NODE_TYPES` |
| `logic.py` | Scoped reference hash (§2.7). At this step nobody holds roles, so role edits stale nobody — correct once step 2 lands |
| `controller.py` | 9 routes following the existing `sections/*` and `clauses/*` shape |
| `facade.py` | `roles()`, `accountabilities()`, `domains()` readers |
| UI | Role cards (§4.2) |

**Done when** a role with accountabilities and domains can be authored,
reordered, diverged and reconciled exactly as clauses can.

### Step 2 — Identity, offers, decisions **[DONE]**

Including the retirement of `agreement_decision`: being part of an
agreement is holding a role in it, so that is what `_has_current_acceptance`
now reads, and the descendant-refusal cascade is gone with it (§2.4).

Actor is still Individual only.

**2a — Identity.** `agreement_identity` node; creator takes it at agreement
creation; adoption predicate (§2.1); divergence rendering; warned
self-install.

**2b — Offers and decisions.** `agreement_role_offer` and
`agreement_role_decision`; default Participant role on every new agreement;
revocation and resignation as authored-node deletion (§2.3); membership
becomes explicit; `acceptance_badges` switches from peers to actors and gains
`unobserved`.

| Area | Work |
|---|---|
| `logic.py` | Identity node + resolution; `offer_role`, `revoke_offer`, `decide_role`, `resign_role`; predicate in `accept_peer_node`; `acceptance_badges` rework |
| `logic.py` | Migrate `agreement_decision` → Participant `agreement_role_decision` |
| `controller.py` | `roles/offer`, `roles/revoke`, `roles/decide`, `roles/resign`, `identity/take` |
| UI | Identity line, offer picker, accept/refuse on role cards, reworked acceptance panel |

**Done when** two sessions can offer, accept, refuse, revoke and resign a
role, and both see consistent badges; and when an Identity handover and a
contested claim both render correctly and resolve through existing
adopt/rollback.

### Step 3a — Agreement as Actor **[DONE]**

The representation, with single-parent behaviour preserved so the existing
suite polices it. `agreement_link` and `parent_agreement_uuid` are gone: a
subagreement is an ordinary role in the parent offered to an Agreement
actor, and an `agreement_role_holding` in the child naming that seat. Both
sides must name the same seat before the relationship exists anywhere.

Three things this surfaced:

- **Adding a subunit no longer re-opens anybody's acceptance.** The seat is
  a role, and roles are outside the document body hash, so the parent's
  text is unchanged by gaining a subagreement. The `agreement_link` it
  replaces *was* document content, which forced everyone to re-accept the
  parent whenever the organisation grew. This also deleted the
  `_reaffirm_holdings` call that existed only to paper over that.
- **The two guards are not the same walk.** `_check_parent_chain` includes
  the agreement being hung from — hanging something below an agreement
  means taking part in it — while `_interaction_guard` excludes the
  agreement being written to, which is why a root is always writable.
  Collapsing them into one walk silently let anybody seat a subagreement
  under an agreement they held nothing in.
- **A holding has to be checkable before it is mounted**, so
  `_holding_is_live` takes the holder rather than looking it up: the
  invited subtree is not in the local index yet, which is the entire point
  of checking it.

### Step 3b — Multiple parents **[DONE]**

An agreement may hold seats in several others. `_ancestry_problem` became an
ANY-path DFS over the holding graph, cycle-safe; home is the first holding in
order whose path validates, derived on read; `seat_agreement` and
`create_seated_agreement` fill a seat with an existing or a new agreement,
both refusing anything that would close a loop.

**The org view stayed a tree.** An earlier draft of this plan called that the
biggest UI change of the whole thing, which was written before home existed
and was wrong afterwards: home edges are a subset of holding edges with at
most one per agreement over a DAG, so the projection is a forest and
`renderOrganization` needed no restructuring at all (2.5). The DAG never
reaches the tree renderer. What it did need is the two markers that keep the
projection honest - `also in:` on a row whose other seats home leaves out,
and a *Seats held* list on the agreement's own page where the order is set.


The graph step. Highest risk — schedule it alone.

| Site | Now | Becomes |
|---|---|---|
| `_check_parent_chain` | `while parent_uuid:` | DFS, ANY valid path, cycle-safe |
| `_interaction_guard` | linear walk | same DFS, memoized |
| `organization_payload` | `parent_for: dict[str,str]`; second link silently dropped; `build()` recurses a tree | holding graph projected through derived home |
| `descendant_agreements` | rejects child whose `parent_agreement_uuid` ≠ parent | holding-based |
| `delete_agreement` | promotes children to roots | removes holdings in this agreement only |
| `accept_agreement_invitation` | single declared parent | ANY parent chain |
| `set_decision` | writes `refused` into every descendant | **deleted** — validity is derived (§2.4) |

**Done when** an agreement holding roles in two parents renders once under its
derived home, annotates the other, survives invalidation of either parent
independently, and falls back in order when home goes invalid.

### Step 4 — Templates

Clone with fresh uuids and zero decisions; template / instantiated / working
state badges. Mostly falls out of §2.8.

## 4. UI

### 4.1 The problem

An agreement now has two faces: **the text** (what we agree) and **the
structure** (who is accountable for what). Roles are both — content you agree
to *and* the membership model. They must not be exiled to an admin panel, and
they must not be styled as prose.

Resolution: roles render as a **distinct region inside the document**, below
sections, as cards rather than paragraphs. Structured content, visibly
structured, but unmistakably part of the thing you are accepting.

The existing shell survives: `aside#organization` left, document pane right,
full re-render per payload.

### 4.2 Role card — definitions and invitations

```
┌─────────────────────────────────────────────────────┐
│ Treasurer                                    ⋯      │
│ Purpose: keep the books honest and current          │
│                                                     │
│ Accountabilities            Domains                 │
│ · Monthly reconciliation    · Bank accounts         │
│ · Filing annual accounts    · Payment approvals     │
│ + add                       + add                   │
│                                                     │
│ Held by  ● Andre  accepted 3 Jul, until 31 Dec      │
│          ○ Maria  offered, not yet decided          │
│          ◌ Ines   unobserved                        │
│ + offer to…                          [Accept][Refuse]│
└─────────────────────────────────────────────────────┘
```

**[DONE]** The region is titled *Role Definitions & Invitations*, and it is
the definition rather than the doing. It sits last: the agreement's text
comes first (collapsible, since once a document is settled the interesting
part is the structure below it), then who is in it — seats held and
participants — then what it expects of them.

- Name and purpose use the existing `editable` in-place pattern — click,
  commit on blur or Enter, revert on Escape. No modal between reader and text.
- Accountabilities and domains reuse the clause affordances: add composer,
  delete, reorder.
- **Held by** lists who has been invited and where each stands. Taking a
  role or stepping out of one is *not* here — it is on your own line in
  Participants (§4.5), so one place answers "what is this role" and
  another answers "what am I doing about it".
- The only controls are the Identity holder's, because inviting and
  withdrawing are theirs alone and have nowhere else to live: an
  `Offer to…` picker, **Confirm** on a request, and **Revoke** where there
  is an offer to withdraw. Confirm is always visible — somebody is waiting
  on you; Revoke waits for a hover, like Delete.
- A vacant role is normal, not an error state — "Nobody invited yet",
  neutral styling.

Confirm cannot be dropped in favour of the picker alone: the picker
excludes anybody already listed under *Held by*, and a requester is listed
there. Without the button a request is unanswerable.

### 4.3 Holder status

Six states, visually distinct, and `unobserved` must not read as `pending`:

| Status | Treatment |
|---|---|
| accepted | solid dot, date and expiry |
| pending | hollow dot, "offered, not yet decided" |
| refused | struck through, muted |
| expired | amber, "lapsed 12 Jun" |
| outdated | amber, "accepted an earlier version" + re-accept action |
| uninvited | *not invited to this agreement yet* — actionable, and not the same as the next one |
| unobserved | greyed italic, tooltip: *you don't sync with this person, so you can't see their decision* |

### 4.4 Identity

**[DONE]** The line only appears when Identity needs attention —
contested, recorded twice, vacant, or held by somebody else and therefore
takeable. When you hold it cleanly it is already visible as your own badge
(§4.5) and the line would be repeating itself.

One line, not a panel:

```
🔑 Identity: Andre                                    ⋯
```

Diverged:

```
⚠ Identity contested — you see Andre, Bob sees Bob
  [Accept Bob's claim]  [Keep Andre]
```

Reuses the existing divergence rendering and the `accept_peer_node` /
`rollback_peer_node` buttons. Handover and contested claim are the same event
(`divergence`) distinguished by *who is asserting what*: if the peer asserting
`holder = Bob` is Bob, it is a handover awaiting your acceptance; otherwise it
is a contested claim. Same event, two renderings — presentation only.

**Take Identity** lives in the overflow menu, never as a primary button, and
goes through the existing `#confirmModal`. The warning states the consequence,
not the mechanism:

> Andre currently holds Identity. Taking it will create a divergence, and
> **your role offers will not be adopted by others** until it is resolved.

### 4.5 Participants — your line acts, everybody else's states

**[DONE]** Replaces the agreement-level acceptance panel. People rows with
their roles as badges, each carrying its own status — somebody may hold
three roles in three different states.

**Your own line comes first and is the only one that acts.** A badge is a
control: click to take the role, click again to step out, and it changes on
the spot. **Refuse is not an action here** — the choice is holding or not
holding.

Everybody else's badges are inert. Where somebody else stands is a
statement of fact, not a control over them, and drawing it as a button
would say otherwise.

Identity renders as an ordinary badge marked with a key (§1.3), so what it
is — one of the roles a person holds — is visible rather than explained.
Actor kinds are drawn differently, so "a person holds this" and "a body
holds this" do not read alike.

Somebody present holding nothing shows as *holds no role here*: visible,
and visibly outside.

### 4.6 Organization tree

`renderOrganization` mostly survives, since the tree renders through derived
home. Additions:

- an agreement with extra holdings shows an `also in: Foundation` marker
- a row whose home fell back shows `home via Foundation` subtly
- template / instantiated badges from §2.8
- the ordered parent list, with reorder, lives on the agreement's own page —
  not in the tree

### 4.7 Creating a sub-agreement becomes filling a seat

The actor picker gains two tabs: **People | Agreements**. Offering a role to
an agreement is what makes it a sub-agreement, so "New subagreement" stops
being a separate button and becomes **"fill this role with a new agreement"**.

Structure is then created the way the model actually works — by filling a
seat — instead of by a parallel affordance that happens to produce the same
nodes.

### 4.8 Rendering — keep the full re-render

The document re-renders fully on every payload, polled every 3s. That stays.

`render()` already refuses to rebuild while the document is being edited
(agreement.html): it returns early when `document.activeElement` is inside
`#document` and is a contenteditable, input, textarea or select. Role cards
inherit this for free.

**Constraint that follows:** no editable surface may live outside
`#document`. `renderOrganization` (`tree.replaceChildren()`) and
`setTopicSelector` both run *before* the guard, unconditionally. This is why
the ordered parent list and its reorder control live on the agreement's own
page and not in the organization tree (§4.6) — a deliberate placement, not an
accident of layout.

**What the guard does not cover** is ephemeral UI state that holds no focus:
open overflow menus, expanded cards, a half-filled offer picker. A 3s rebuild
closes them. The fix is not DOM patching but **holding that state in JS rather
than in the DOM** — an object keyed by role uuid, reapplied on render. Full
re-render then stays viable by construction.

Plus one skip: every payload already carries `"revision"`
(`application_composite_response`, application.py), but do **not** gate on it.
It is a Session revision, and `merge_document_observation` decorates the
snapshot with peer liveness *after* `read_snapshot`, so transport changes may
not advance it and the network indicators would freeze. Compare a JSON string
of the last payload instead and skip `render()` when identical — one stringify
per poll at this document size, and no dependence on every mutation path
advancing the revision correctly.

**Rejected for now:** the keyed reconcile helper s-kanban already has
(`reconcileDOM(parent, dataItems, keyFn, createFn, updateFn)`, kanban.html).
Copyable and proven, but it needs a `createFn`/`updateFn` pair per node type —
five new pairs here — and a pair falling out of step is a silent bug class this
codebase has already met. Revisit only if role cards grow state that genuinely
cannot live outside the DOM.

## 5. Open

- **[DONE]** Reads memoise inside a scope. Building one payload asked
  Session for the same identity, member lists, node lookups and content
  hashes hundreds of times — `Session.identity` alone was read 349 times, and
  each read snapshots the whole protocol tree — which cost **1125 ms per
  build** against a 3-second poll. Scoped to a single read it is **67 ms**.
  The scope is the read and nothing outside one caches, so a mutation can
  never be served a stale entry; keying it on the view revision instead was
  tried first and was wrong, because logic-level mutations do not advance it.
- **[OPEN]** Whether two roles claiming the same domain in one agreement
  should be detected as a conflict. Out of scope for the first cut; named so
  it is not later mistaken for a bug.
- **[RESOLVED]** Whether a background payload refresh can destroy an
  in-progress inline edit. It cannot — see §4.8.
- **[OPEN]** Role as a third Actor kind (a role holding a seat in a
  sub-agreement, so the seat survives personnel change). Deliberately deferred
  — `actor_ref` is a discriminated union so this stays additive.
- **[DONE]** How a newly invited person gets their first role — resolved in
  §2.5 below. Retiring `agreement_decision` is now unblocked but not done:
  role holdings still run alongside it, and `_interaction_guard` still reads
  the old record.
