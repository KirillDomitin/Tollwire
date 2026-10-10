# Security policy

Tollwire holds SSH credentials and VPN client keys, so security reports are taken seriously.

## Reporting a vulnerability

**Please do not open a public issue for a security problem.**

Use GitHub's private reporting instead: open the **Security** tab of this repository and choose
**Report a vulnerability**. If that is not available, contact the maintainer directly through the e-mail address
on their GitHub profile.

Include, if you can:

- what is affected (component, endpoint, version or commit);
- steps to reproduce, or a proof of concept;
- what an attacker gains.

You will get an acknowledgement as soon as the maintainer can respond, and a fix or a decision before the
report is made public. Please give a reasonable time to fix the problem before disclosing it.

## Supported versions

The project has not had a tagged release yet. Security fixes go to the `master` branch.

## What is in scope

- authentication, sessions and role checks;
- leaking secrets: SSH credentials, VPN configs, keys, tokens (in responses, logs or the audit log);
- injecting input into SSH commands or SQL;
- anything that lets one admin exceed the permissions of their role.

## Deploying safely

- Keep `.env.prod`, `certs/` and `ENCRYPTION_KEY` out of git and away from other people.
- Expose only nginx to the internet; PostgreSQL and Redis stay on the internal network.
- Run behind HTTPS; the refresh cookie is `Secure` by default.
- Back up `ENCRYPTION_KEY` together with the database, but not on the same server.
