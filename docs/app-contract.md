# The UVMC app contract (version 1)

An app built to this contract is a normal, standalone program: it runs on any similar Linux
without UVMC. UVMC is one host for it (a **service**, see [guide.md](guide.md#services)); a plain
Linux machine, a container or your PC are others. The contract is what the app and its host
promise each other, so the same code runs everywhere and nothing in it depends on UVMC, except
the optional client library, which works without UVMC too.

## 1. The manifest: `uvmc-app.toml`

At the top of the app's repository (or a folder you name when adding it). It describes the app;
it is not code. UVMC reads it to build the app's walls and to show you what you approve. Elsewhere
it is documentation of what the app needs.

```toml
contract = 1                               # this version of the contract
name = "tidebreak-dispatcher"              # 2-32 lowercase letters, digits, dashes
title = "Training queue dispatcher"
description = "Fills free training slots from jobs/queue.json"
run = "python3 tools/dispatcher.py"        # starts the app, from its folder; it runs until stopped
restart = "on-failure"                     # or "always": when the host starts it again
memory = "256M"                            # its memory limit
health = "python3 tools/dispatcher.py --check"   # optional: exit 0 = healthy

[network]
sites = ["github.com"]                     # web sites it reaches (https/http), subdomains included
ssh = ["mac"]                              # machines it reaches by SSH, by name (tables below)

[ssh.mac]
host = "10.0.1.226"                        # address or host name
user = "remote-worker"                     # the user on that machine
key = "mac_key"                            # the secret setting holding its private key

[[settings]]                               # one table per setting
name = "github_token"                      # lowercase letters, digits, _
label = "GitHub token"                     # what you see in the form
type = "secret"                            # text, secret, number, yes/no or long text
required = true                            # default true
# default = "..."                          # for text, number, yes/no
```

Nothing else is reachable: no site, port or machine that isn't declared here.

### Versions of the contract

- `contract` is the version the manifest is written for. A manifest for a higher version than this
  UVMC knows is refused with "update UVMC first": never run half set up.
- Keys planned for later versions are refused with a clear message, not ignored: `build` (a build
  step), `files` (config files from settings), `env` (settings under names of the app's choosing),
  `web` (a page shown in the panel), `image`/`container`, `volumes`, `ports`, `depends`.
- `runtime` is `"process"` (the default, the only one in version 1); `"container"` is planned.
- Any other key is kept, shown on the approval card as unknown, and ignored.

### Apps without a manifest

Most apps on git don't have a `uvmc-app.toml`. You can write one for such an app in UVMC: it's kept by
UVMC (the app's repository stays as it is) and works exactly like the repository's own. UVMC calls it
the service's **profile**; it can also replace the repository's own. Changing it waits for your approval,
like a new version.

## 2. What the app gets

The same names on every host:

| What | How |
|---|---|
| Settings (text, number, yes/no) | environment variables `APP_<NAME>` (`APP_GITHUB_TOKEN` style, upper case) |
| Secrets and long texts | one file each, named after the setting, in the folder `$CREDENTIALS_DIRECTORY`; read them when needed, don't copy them elsewhere |
| Its data folder | `$APP_DATA`: kept between restarts and updates; the app's working state lives here (a git checkout it works on, a database, caches) |
| SSH machines | by name: `ssh mac`, through `~/.ssh/config`, which the host provides (address, user, key, pinned host key) |
| Web | the standard proxy variables (`HTTPS_PROXY` ...); tools that honour them (git, pip, curl, Python's urllib) just work |
| Logs | plain standard output and error |
| Stopping | `SIGTERM`, then up to 15 seconds to finish |
| UVMC (optional) | `UVMC_URL` and the key in `$CREDENTIALS_DIRECTORY/uvmc-key`, used through the client library |

## 3. What the app must not assume

- root, a particular user name, or a writable home folder other than `$APP_DATA`;
- writing anywhere but `$APP_DATA` and the system's temporary folder (its own, emptied when it stops);
- reaching anything not declared in the manifest;
- that its code folder is writable (it is read-only under UVMC);
- a terminal or an interactive question.

## 4. The optional UVMC API

Through [client/python/uvmc_client.py](../client/python/uvmc_client.py) (one file, standard library only):

```python
from uvmc_client import Uvmc
uvmc = Uvmc()            # UVMC_URL, the uvmc-key credential (or UVMC_KEY_FILE / UVMC_KEY), APP_DATA or UVMC_DATA
uvmc.report(title="Queue", status=("ok", "3 running"), blocks=[uvmc.table(["Job", "State"], rows)])
uvmc.heartbeat()
```

Without UVMC, reports go to `$APP_DATA/reports/latest.json` and nothing fails. More features
(asking you a question, offering actions to agents) come in later versions of this contract.

## 5. Running it without UVMC

On any similar Linux (or to find out whether a problem is the app's or a wall's):

```bash
mkdir -p secrets data
printf '%s\n' "<token>" > secrets/github_token          # one file per secret
export CREDENTIALS_DIRECTORY=$PWD/secrets APP_DATA=$PWD/data APP_GREETING=hello
# a normal ~/.ssh/config with Host entries named like the manifest's machines
python3 tools/dispatcher.py
```

If it works like this and not in UVMC, the difference is a wall: the service's page in UVMC names
every refused connection and why.

## Under UVMC

What UVMC does with an app (details in [guide.md](guide.md#services)): fetches the repository,
shows you the manifest, the commit and its files, and installs only what you approve; runs it as
its own user from a read-only copy of that commit, as a systemd service inside systemd's sandbox
and the services' resource box; reaches only the declared sites and machines through a proxy of
its own; hands secrets over as systemd credentials; writes the ssh config with the host keys you
gave; gives it a program key. New commits wait for your approval, with their changes shown.
