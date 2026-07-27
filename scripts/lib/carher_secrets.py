#!/usr/bin/env python3
"""Load operator secrets from a LOCAL config file that is never committed.

Why this exists: the 198 sudo password was hardcoded in four tracked files. It was
never pushed to GitHub (verified), but a plaintext production credential in the
working tree is one `git push` away from permanent disclosure, and four copies
means a rotation has to find all four.

Resolution order for any key (first hit wins):
  1. the environment  (e.g. SUDO_PW=... ) -- best for CI and one-off overrides
  2. $CARHER_SECRETS  -- explicit path override
  3. ./.carher-secrets.json           (repo root, gitignored)
  4. ~/.config/carher/secrets.json    (per-user, outside the repo)

File format is flat JSON:

    {
      "SUDO_PW": "...",          # sudo password on 10.68.13.198 / .225
      "LITELLM_MASTER_KEY": "...",
      "ZK_KEY": "sk-..."         # a LiteLLM virtual key, for test harnesses
    }

Create it with `scripts/lib/carher-secrets-init.sh`, or by hand with mode 600.
"""
import json
import os
import sys

_CACHE = None


def _candidate_paths():
    paths = []
    explicit = os.environ.get("CARHER_SECRETS")
    if explicit:
        paths.append(explicit)
    # repo root = two levels up from scripts/lib/
    here = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.dirname(os.path.dirname(here))
    paths.append(os.path.join(repo_root, ".carher-secrets.json"))
    paths.append(os.path.expanduser("~/.config/carher/secrets.json"))
    return paths


def _load():
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    _CACHE = {}
    for p in _candidate_paths():
        if not p or not os.path.isfile(p):
            continue
        try:
            with open(p, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                # First file wins, but later files can fill in missing keys.
                for k, v in data.items():
                    _CACHE.setdefault(k, v)
                _CACHE.setdefault("__source__", p)
        except Exception as e:
            print("[carher_secrets] ignoring %s: %s" % (p, e), file=sys.stderr)
    return _CACHE


def get(name, default=None):
    """Environment first, then the local config file."""
    v = os.environ.get(name)
    if v:
        return v
    return _load().get(name, default)


def require(name, hint=""):
    """Same, but abort with an actionable message instead of failing later with a
    confusing auth error."""
    v = get(name)
    if v:
        return v
    sys.exit(
        "missing secret %r.\n"
        "  Set it one of these ways:\n"
        "    export %s='...'\n"
        "    echo '{\"%s\": \"...\"}' > .carher-secrets.json && chmod 600 .carher-secrets.json\n"
        "    or run scripts/lib/carher-secrets-init.sh\n"
        "  (.carher-secrets.json is gitignored and must never be committed.)%s"
        % (name, name, name, ("\n  " + hint) if hint else ""))


def source_path():
    """Which file the secrets came from, for diagnostics. None if env-only."""
    return _load().get("__source__")


if __name__ == "__main__":
    # Diagnostic: report what resolves, WITHOUT printing any value.
    src = source_path()
    print("secrets file: %s" % (src or "(none found -- env only)"))
    for k in ("SUDO_PW", "LITELLM_MASTER_KEY", "ZK_KEY"):
        v = get(k)
        print("  %-20s %s" % (k, ("set (%d chars)" % len(v)) if v else "MISSING"))
