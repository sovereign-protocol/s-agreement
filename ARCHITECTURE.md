# Architecture

S-Team owns agreement node schemas (`agreement`, `agreement_section`,
`agreement_clause`, `agreement_role` and its items, `agreement_role_offer`,
`agreement_role_decision`, `agreement_role_holding`, `agreement_identity`), its
proposal and adoption policy, controllers, facade, and browser UI. It imports only the documented `sovereign` package root. Sovereign
Core owns protocol, Session, channels, hosting, identity, and blob mechanics and
contains no agreement node-type knowledge — a rule Core enforces in its own
suite by scanning its source for agreement vocabulary.

Taking part is a role, not a membership list. An `agreement_role` carries
`agreement_accountability` and `agreement_domain` items, and beneath it two
independent records per actor: an `agreement_role_offer` written by the
agreement's Identity holder, and an `agreement_role_decision` written by the
actor. A role is held only while both exist, so an offer alone is an unfilled
seat and a decision alone is a request to take one. A decision carries the
decision time, an optional expiry, and a SHA-256 reference hash over the
document body plus that role's own definition — scoped that way so editing one
role does not re-open everybody else's acceptance. Who speaks for an agreement
is a single `agreement_identity` node naming the holder; that one is singular
rather than an offer-and-answer pair, because both replicas naming the same
holder is itself the agreement.

Organizational relationships are the same shape. A subagreement is an
Agreement actor holding a role in its parent: the parent side is an ordinary
role with an offer whose `actor_kind` is `agreement`, and the child side is an
`agreement_role_holding` naming that parent and role. An agreement may hold
seats in several others, so the relationships form a DAG; it is *drawn* as a
tree by projecting through home, the first holding in order that reaches a
root. The child remains beside the parent under the application container
rather than inside its protocol subtree, preserving independent invitations,
channels, membership, and deletion. Neither side of a seat may be removed by
deleting the other's record: giving one up withdraws this side's answer and
leaves the parent's role and offer standing.

Decision, offer, holding, and identity nodes are records *about* the agreement
rather than content of it, so they stay out of the document serialization and
out of the body hash, and are rendered as identity badges rather than
document-change proposals. A child topic mounts only when the local
participant has a current, unexpired acceptance for every ancestor.

Where it differs from S-Initiative: a peer's node that has no local counterpart is
presented as a *proposal* to accept or withdraw, rather than merged and then
reconciled. A document is a thing people agree to before it is true, so the
adoption step is the point rather than an obstacle.
