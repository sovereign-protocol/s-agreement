# Architecture

S-Agreement owns agreement node schemas (`agreement`, `agreement_section`,
`agreement_clause`), its proposal and adoption policy, controllers, facade, and
browser UI. It imports only the documented `sovereign` package root. Sovereign
Core owns protocol, Session, channels, hosting, identity, and blob mechanics and
contains no agreement node-type knowledge — a rule Core enforces in its own
suite by scanning its source for agreement vocabulary.

Where it differs from S-Kanban: a peer's node that has no local counterpart is
presented as a *proposal* to accept or withdraw, rather than merged and then
reconciled. A document is a thing people agree to before it is true, so the
adoption step is the point rather than an obstacle.
