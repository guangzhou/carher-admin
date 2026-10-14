"""Exchange an Antigravity OAuth callback, probe daily generateContent with both UAs, and save
creds + validation_url when the account needs Google's 'Verify your account' step.

Run on 198 as root from /root/cliproxy-manifests (reuses constants from cliproxy-antigravity-add-account.py).
Usage: python3 ag_exchange_probe.py <name> < callback_url
Writes /root/cliproxy-manifests/pending/<name>.creds.json and <name>.validation_url (600).
"""
import importlib.util, json, os, sys

spec = importlib.util.spec_from_file_location("ag", "/root/cliproxy-manifests/cliproxy-antigravity-add-account.py")
ag = importlib.util.module_from_spec(spec); spec.loader.exec_module(ag)
SECRET = ag.client_secret() if hasattr(ag, "client_secret") else ag.CLIENT_SECRET
name = sys.argv[1]
code = ag.parse_code(sys.stdin.read())
st, tok = ag.http_json(ag.TOKEN_EP, {"client_id": ag.CLIENT_ID, "client_secret": SECRET, "code": code,
                                     "grant_type": "authorization_code", "redirect_uri": ag.REDIRECT_URI}, form=True)
if st != 200 or "refresh_token" not in tok:
    sys.exit("token exchange failed %s %s" % (st, json.dumps(tok)[:200]))
email = ag.jwt_email(tok["id_token"])
at = tok["access_token"]
hub = ag.hub_user_agent()
res = {}
for ua in (hub, ag.UA_WORKING):
    for model in ("gemini-3.8-flash-high", "gemini-3.8-flash-tiered", "gemini-pro-agent"):
        ok, hc, body = ag.generate_probe(at, ag.DEFAULT_PROJECT, ua, model)
        res[(ua, model)] = (ok, hc)
        print("%-40s %-24s ok=%s http=%s" % (ua, model, ok, hc))
        if hc == 403 and "validation_url" in body and "validation_url" not in res:
            try:
                d = json.loads(body if body.endswith("}") else body + "\"}}]}}")
            except Exception:
                d = None
            res["validation_body"] = body
D = "/root/cliproxy-manifests/pending"; os.makedirs(D, exist_ok=True); os.chmod(D, 0o700)
# Re-fetch the full 403 body (generate_probe truncates to 300 chars) to extract validation_url.
code_, full = ag.http_json(ag.DAILY + "/v1internal:generateContent",
                           {"model": "gemini-3.8-flash-high", "project": ag.DEFAULT_PROJECT,
                            "request": {"contents": [{"role": "user", "parts": [{"text": "hi"}]}]}},
                           {"Authorization": "Bearer " + at, "User-Agent": hub})
vurl = None
for det in (full.get("error", {}).get("details", []) if isinstance(full, dict) else []):
    vurl = vurl or (det.get("metadata") or {}).get("validation_url")
creds = {"type": "antigravity", "email": email, "project_id": ag.DEFAULT_PROJECT, "access_token": at,
         "refresh_token": tok["refresh_token"], "expires_in": tok.get("expires_in", 3599), "timestamp": 0,
         "expired": "2020-01-01T00:00:00Z", "disabled": False}
p = os.path.join(D, name + ".creds.json")
with open(p, "w") as f: json.dump(creds, f)
os.chmod(p, 0o600)
if vurl:
    with open(os.path.join(D, name + ".validation_url"), "w") as f: f.write(vurl)
    os.chmod(os.path.join(D, name + ".validation_url"), 0o600)
print("email=%s saved_creds=%s validation_url=%s" % (email, p, "yes (%s...)" % vurl.split("?")[0] if vurl else "none"))
