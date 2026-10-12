# Security policy

## Supported versions

Only the latest release gets security fixes. Upgrade with `sudo apt upgrade`, by installing the
latest `.deb` or `.rpm`, or with `uv tool install --reinstall`.

## Reporting a vulnerability

Please don't open a public issue. Report it privately through
[GitHub's vulnerability reporting](https://github.com/ronaldorcampos/corewatch/security/advisories/new).

Include what's affected (version and how it was installed), how to reproduce it, and what an
attacker gains. Once a fix is released, the advisory is published, crediting you unless you'd
rather not be.

## Scope

In scope: corewatch itself, the `.deb` and `.rpm` packages and what they install, the apt
repository and its signing key, and the release workflow that builds them.

Not a vulnerability in corewatch:

- **The CPU power udev rule.** `packaging/99-corewatch-rapl.rules` makes the RAPL energy counters
  readable by every user, which exposes the power side channel of CVE-2020-8694 ("PLATYPUS"). That
  is its documented purpose, and it's opt-in: the packages ship it without installing it. Leave
  it out on a machine shared with users you don't trust.
- Bugs in the kernel drivers, the NVIDIA driver or its libraries, Qt, or other dependencies.
  Report those upstream; if corewatch needs to change because of one, an issue here is welcome.
