# Security

S-Agreement alpha is intended for trusted peers and LAN/VPN use. Direct HTTP is
not an Internet-facing security boundary. Connect tokens are Base64, not
encryption. Experimental SFTP descriptors may contain bearer credentials; use a
dedicated, jailed, least-privilege relay account and never commit local
configuration.

An adopted agreement is a record of what peers saw and accepted, not a signed
or tamper-evident document. Do not treat it as evidence against a peer who
controls their own copy.

Report vulnerabilities through GitHub private vulnerability reporting when
enabled, not a public issue.
