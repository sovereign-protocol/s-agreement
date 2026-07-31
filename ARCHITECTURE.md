# Architecture

S-Agreement owns agreement node schemas (`agreement`, `agreement_section`,
`agreement_clause`), its proposal and adoption policy, controllers, facade, and
browser UI. It imports only the documented `sovereign` package root. Sovereign
Core owns protocol, Session, channels, hosting, identity, and blob mechanics and
contains no agreement node-type knowledge — a rule Core enforces in its own
suite by scanning its source for agreement vocabulary.

Organizational relationships use two independent consent records. A parent
topic contains an `agreement_link` naming the child, and the child agreement
root names its intended parent. The child remains beside the parent under the
application container rather than inside its protocol subtree, preserving
independent invitations, channels, membership, and deletion.

Participant decisions are `agreement_decision` children of the agreement
topic. They are separate from document sections and contain the participant
identity, decision time, optional expiry, and a SHA-256 hash over agreement
content excluding decision and agenda nodes. Decision nodes are rendered as
identity badges rather than document-change proposals. A child topic mounts
only when the local participant has a current, unexpired acceptance for every
ancestor and the corresponding parent links are locally accepted.

Where it differs from S-Kanban: a peer's node that has no local counterpart is
presented as a *proposal* to accept or withdraw, rather than merged and then
reconciled. A document is a thing people agree to before it is true, so the
adoption step is the point rather than an obstacle.
