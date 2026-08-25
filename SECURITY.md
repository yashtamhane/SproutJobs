# Security

## Reporting a security issue

Please report suspected vulnerabilities **privately**, through this
repository's **Security → Report a vulnerability** (GitHub Private
Vulnerability Reporting) form. If that is unavailable to you, open an issue
that says only that you have found a security problem and asks for a private
channel — please do not include details in a public issue.

Please include what you can: what you observed, how to reproduce it, and what
you think the impact is. You will get an acknowledgement as soon as a
maintainer sees it. This is a volunteer project with no paid on-call, so please
do not expect an SLA — but a real report will get a real response.

There is no bug bounty.

## Threat model, honestly stated

SproutJobs is a **single-user, local-first desktop tool**. It is a program you
run on your own machine to read public data and put it in a local file. It has
no accounts, no multi-tenancy, no privilege boundaries, and no remote
components. Please calibrate reports accordingly.

## The dashboard binds to 127.0.0.1

The local dashboard listens on `127.0.0.1:8765` by default. That address is
reachable only from your own machine.

**It is not intended to be exposed to the internet, and it must not be.**
Specifically it has:

- **no authentication and no authorization** — anyone who can reach the port can
  read everything in your database;
- **no TLS** — it speaks plain HTTP;
- **no CSRF protection, rate limiting, or abuse controls** — it is a read-only
  GET-only server for one trusted user.

Do not bind it to a public interface, do not port-forward it, and do not put it
behind a tunnel or reverse proxy that makes it publicly reachable. If you need
it on another device, use an SSH tunnel and keep the listener on loopback.

`python -m jobfinder compliance` verifies that the loopback default is intact.

## No credentials are stored

SproutJobs needs no API key, token, password or account, and it reads none from
your environment. It has no login flow and sends no credentials anywhere.

**Do not add credentials to `config.toml`.** Nothing in the tool will use them,
and `config.toml` is a tracked file — anything you put there is one `git commit`
away from being public. `.gitignore` covers `.env`, `*.pem` and `*.key` as a
backstop, but the real answer is that this tool does not want your secrets.

## Generated runtime data is local, and should stay that way

Running SproutJobs writes `data/jobfinder.sqlite3`, a discovery cache, and
exports under `output/`. These contain **copied text from public job postings**,
sometimes tens of thousands of them.

- They are covered by `.gitignore` and must not be committed.
- They are not yours to redistribute — the postings belong to their employers.
- Keep them local. Do not publish the database, the CSV, or the HTML export.
- The same applies to screenshots: a screenshot of your dashboard contains real
  job text and real company names. Think before posting one.

## Reasonable dependency surface

The runtime dependency is `requests`; `pytest` is used for tests. Everything
else is the Python standard library. The dashboard has no JavaScript
dependencies, no CDN, and no build step. `python -m jobfinder compliance` fails
if a dependency outside that set appears.

## What is out of scope

- The dashboard being unauthenticated when you have deliberately exposed it to a
  network. That is documented above, not a vulnerability.
- Anything requiring an attacker who already has code execution or filesystem
  access as your user.
- The heuristic scoring being wrong about a job. That is a bug — file it
  normally, in public.
