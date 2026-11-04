"""Post-deploy: render app.yml, grant the app SP read access, provision Lakebase
(per-user history), and (re)deploy the app.

Run AFTER `databricks bundle deploy`.

Usage:
    export DATABRICKS_PROFILE=<profile>
    export TARGET=dev            # bundle target: dev (default) | stg | prod.
                                 # The app name is fixed per target in
                                 # databricks.yml and read back from the bundle;
                                 # do NOT pass an app name. (An APP_NAME env is
                                 # IGNORED — only warned about — so it can never
                                 # redirect grants to another environment; the
                                 # target is the sole source of truth.)
    export WAREHOUSE_ID=<warehouse_id>
    # optional:
    #   export ASSESS_CATALOGS="cat_a,cat_b"
    #   export GENIE_SPACE_ID=<id>
    #   export BRAND_NAME="Acme"
    #   export FORCE_SP=true                         # SP-only mode (never attempt OBO)
    #   export USE_LAKEBASE=true                     # enable per-user history/plans
    #   export LAKEBASE_INSTANCE_NAME=<instance>     # existing Lakebase instance to reuse
    #   export LAKEBASE_DATABASE=ontology_readiness
    python3 scripts/post_deploy.py

Python 3.9+. Requires databricks-sdk>=0.36.0 (the SP-grant step,
setup_app_permissions.py, resolves the app SP via the SDK). The Lakebase
history steps additionally require asyncpg.
"""

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent / "app"

# Reuse the app's git-tag version resolver (dependency-light — stdlib only) so
# the baked version matches what the running app reports.
sys.path.insert(0, str(APP_DIR))
from server.user_agent import git_version  # noqa: E402

PROFILE = os.environ.get("DATABRICKS_PROFILE", "")
# Bundle target (dev|stg|prod). The app name is pinned per target in
# databricks.yml and resolved from the bundle in main(); TARGET — not an app
# name — is what selects the environment.
TARGET = os.environ.get("TARGET", "dev")
# The bundle targets defined in databricks.yml. Kept here to validate TARGET and
# to derive a deterministic fallback app name (prod -> bare, else -<target>) that
# matches the yaml, so a failed `bundle summary` never silently falls back to the
# live prod app name for a dev/stg deploy.
VALID_TARGETS = ("dev", "stg", "prod")
# Placeholder only — main() reassigns this via resolve_app_name(), which resolves
# the name from the bundle target. An explicit APP_NAME env is IGNORED there (only
# warned about), never honored, so it cannot redirect this run to another env.
APP_NAME = os.environ.get("APP_NAME", "genie-ontology-readiness")
WAREHOUSE_ID = os.environ.get("WAREHOUSE_ID", os.environ.get("DATABRICKS_WAREHOUSE_ID", ""))
USE_LAKEBASE = os.environ.get("USE_LAKEBASE", "false").lower() == "true"
LAKEBASE_INSTANCE = os.environ.get("LAKEBASE_INSTANCE_NAME", "")
LAKEBASE_DATABASE = os.environ.get("LAKEBASE_DATABASE", "ontology_readiness")
if not re.fullmatch(r"[a-z_][a-z0-9_]*", LAKEBASE_DATABASE):
    sys.exit(f"ERROR: LAKEBASE_DATABASE={LAKEBASE_DATABASE!r} must match [a-z_][a-z0-9_]*")
# These provisioning connections carry a live Lakebase credential over the public
# internet. `require` encrypts but verifies nothing, so it gives no protection
# against an in-path attacker — see app/server/lakebase_client.py for the runtime
# counterpart and the escape hatch.
LAKEBASE_SSL_MODE = os.environ.get("LAKEBASE_SSL_MODE", "verify-full")
MAX_WAIT = 600


def _lakebase_ssl():
    """asyncpg ``ssl`` arg for LAKEBASE_SSL_MODE (mirrors lakebase_client._ssl_arg).

    Passing the bare ``verify-full``/``verify-ca`` string makes asyncpg look for a
    CA cert at ``~/.postgresql/root.crt`` (absent here), so the provisioning
    connections fail. The Lakebase endpoint is publicly-signed, so build an
    SSLContext from a trusted CA bundle and verify against it; non-verifying modes
    pass through as the plain string."""
    import ssl
    mode = (LAKEBASE_SSL_MODE or "").lower()
    if mode not in ("verify-ca", "verify-full"):
        return LAKEBASE_SSL_MODE
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except Exception:
        ctx = ssl.create_default_context()
    if mode == "verify-ca":
        ctx.check_hostname = False
    return ctx

# Populated by setup_lakebase() when USE_LAKEBASE is on.
_LAKEBASE = {"host": "", "sp_client_id": ""}


def render_app_yml():
    """Write app/app.yml from the template with env-driven values."""
    template = APP_DIR / "app.yml.template"
    out = APP_DIR / "app.yml"
    text = template.read_text()

    def set_env(name: str, value: str) -> None:
        nonlocal text
        pattern = rf'(- name: {name}\n\s+value: ")[^"]*(")'
        safe = value.replace("\\", "\\\\").replace('"', '\\"')
        text, n = re.subn(pattern, lambda m: m.group(1) + safe + m.group(2), text)
        if n == 0:
            print(f"  (warning) could not set {name} in app.yml — leaving template default")

    set_env("ASSESS_CATALOGS", os.environ.get("ASSESS_CATALOGS", ""))
    set_env("GENIE_SPACE_ID", os.environ.get("GENIE_SPACE_ID", ""))
    set_env("WORKSPACE_ID", _resolve_workspace_id())
    # Bake the released version into the app's env (the deployed app has no .git),
    # so its Databricks API calls carry it in the User-Agent. Empty is fine — the
    # app then resolves its own fallback (see user_agent.py).
    set_env("GOR_VERSION", git_version() or "")
    set_env("BRAND_NAME", os.environ.get("BRAND_NAME", "Databricks"))
    set_env("FORCE_SP", os.environ.get("FORCE_SP", "false"))
    set_env("USE_LAKEBASE", "true" if (USE_LAKEBASE and _LAKEBASE["host"]) else "false")
    set_env("LAKEBASE_HOST", _LAKEBASE["host"])
    set_env("LAKEBASE_USER", _LAKEBASE["sp_client_id"])
    set_env("LAKEBASE_DATABASE", LAKEBASE_DATABASE)
    set_env("LAKEBASE_SSL_MODE", LAKEBASE_SSL_MODE)
    set_env("LAKEBASE_INSTANCE_NAME", LAKEBASE_INSTANCE)

    out.write_text(text)
    print(f"  Rendered {out}")


def _resolve_workspace_id() -> str:
    """The deployed workspace's id, to seed the app's default workspace filter.

    Honors an explicit WORKSPACE_ID env if set; otherwise resolves it from the CLI
    (``databricks metastores current`` returns the current workspace_id). Falls back
    to empty (the app then defaults the filter to all workspaces) if unavailable."""
    explicit = os.environ.get("WORKSPACE_ID", "")
    if explicit:
        return explicit
    ms = cli_json("metastores", "current")
    if isinstance(ms, dict) and ms.get("workspace_id"):
        return str(ms["workspace_id"])
    print("  (warning) could not resolve WORKSPACE_ID; workspace filter will default to all workspaces")
    return ""


def cli_json(*args):
    cmd = ["databricks"] + list(args) + ["--output", "json"]
    if PROFILE:
        cmd += ["--profile", PROFILE]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return r.stdout.strip()


def cli(*args, check=True):
    cmd = ["databricks"] + list(args)
    if PROFILE:
        cmd += ["--profile", PROFILE]
    print(f"  $ {' '.join(cmd)}")
    result = subprocess.run(cmd, text=True)
    if check and result.returncode != 0:
        print(f"  command failed ({result.returncode})")
    return result.returncode


def deployment_status(app_name):
    """Return (app_state, deployment_state, deployment_message, url, source_path) from `apps get`.

    `bundle run` on an app resource only reports a non-zero exit; the reason lives
    in active_deployment.status.message (e.g. "Error installing packages"). Read it
    so the operator sees the actual failure instead of a guess."""
    app = cli_json("apps", "get", app_name)
    if not isinstance(app, dict):
        return "", "", "", "", ""
    # pending_deployment first: after a failed redeploy of a running app,
    # active_deployment is still the previous *successful* one.
    dep = app.get("pending_deployment") or app.get("active_deployment") or {}
    dep_status = dep.get("status") or {}
    app_status = app.get("app_status") or {}
    return (app_status.get("state", ""), dep_status.get("state", ""),
            dep_status.get("message", ""), app.get("url", ""),
            dep.get("source_code_path", ""))


def explain_deploy_failure(app_name, dep_state, dep_message, source_path=""):
    """Print what failed and exactly where the evidence is.

    The install log is only reachable with a user OAuth token (CLI `apps logs` or
    the app's /logz page), never through the REST API, so point the operator
    there rather than speculating about the cause."""
    print(f"\n  DEPLOYMENT {dep_state or 'FAILED'}: {dep_message or '(no status message)'}")
    if "installing packages" in (dep_message or "").lower():
        print("  The Apps runtime failed while running `pip install -r requirements.txt`.")
        print("  This is a dependency or network problem inside the app container, not a")
        print("  bundle variable problem. The first error line in the log decides which:")
        print("    - 'Could not fetch URL' / timeout  -> workspace egress blocks PyPI")
        print("    - 'Failed building wheel for X'     -> no prebuilt wheel for the runtime; pin X lower")
        print("    - 'No matching distribution for X'  -> pin does not exist for Python 3.11")
        print("    - 'ResolutionImpossible'            -> conflicting pins; relax the named ones")
    print("  Read the log (needs your OAuth profile, not a PAT):")
    print(f"    databricks apps logs {app_name}" + (f" --profile {PROFILE}" if PROFILE else ""))
    print("    or open <app URL>/logz in a browser.")
    print("  After fixing, redeploy the already-synced code without re-running Terraform:")
    print(f"    databricks apps deploy {app_name} --source-code-path "
          f"{source_path or '<active_deployment.source_code_path>'}"
          + (f" --profile {PROFILE}" if PROFILE else ""))


def update_app(extra_resources=()):
    """Re-assert the `sql` user scope (OBO) and add any extra resources, preserving
    the app's existing resources and description. `bundle deploy` only applies
    user_api_scopes on create, so an app created before the scope existed needs
    this on every run, Lakebase or not. Returns True on success."""
    app = cli_json("apps", "get", APP_NAME)
    if not isinstance(app, dict):
        print(f"  WARNING: could not read app {APP_NAME}; user_api_scopes not asserted")
        return False
    resources = []
    for r in app.get("resources", []):
        item = {"name": r["name"]}
        for k in ("sql_warehouse", "database", "secret", "serving_endpoint", "job"):
            if r.get(k) is not None:
                item[k] = r[k]
        resources.append(item)
    names = {r["name"] for r in resources}
    resources += [r for r in extra_resources if r["name"] not in names]
    payload = {"name": APP_NAME, "resources": resources, "user_api_scopes": ["sql"]}
    if app.get("description"):
        payload["description"] = app["description"]
    # Retry: `apps update` can transiently conflict right after `bundle run`.
    for _ in range(6):
        if cli_json("apps", "update", APP_NAME, "--json", json.dumps(payload)):
            print(f"  user_api_scopes: sql   resources: {', '.join(r['name'] for r in resources)}")
            return True
        time.sleep(10)
    print("  WARNING: apps update failed after retries (user_api_scopes / resources not applied)")
    return False


def _fallback_app_name():
    """Deterministic name for TARGET matching the databricks.yml convention.

    Used only when `bundle summary` can't be read; it mirrors the yaml (prod ->
    bare, dev/stg -> suffixed) so a transient failure never resolves a dev/stg
    deploy to the live prod app name."""
    return "genie-ontology-readiness" if TARGET == "prod" else f"genie-ontology-readiness-{TARGET}"


def resolve_app_name():
    """Read the target's pinned app name back from the bundle.

    The name lives ONLY in databricks.yml (per-target resource override), so the
    bundle target — never a caller-supplied APP_NAME — is the source of truth and
    this script can't disagree with the actual deploy. If `bundle summary` can't
    be read, fall back to the target-derived name (not the bare prod name). An
    explicit APP_NAME env is ignored (with a warning) — it exists only as a last
    resort and must never silently redirect grants to another environment."""
    args = ["bundle", "summary", "-t", TARGET]
    if WAREHOUSE_ID:
        args += ["--var", f"warehouse_id={WAREHOUSE_ID}"]
    summ = cli_json(*args)
    try:
        name = summ["resources"]["apps"]["ontology_readiness"]["name"]
    except (TypeError, KeyError):
        name = None
    if not name:
        name = _fallback_app_name()
    explicit = os.environ.get("APP_NAME")
    if explicit and explicit != name:
        print(f"  (warning) APP_NAME={explicit!r} ignored — target '{TARGET}' resolves the "
              f"app name to {name!r}; the target is the source of truth.")
    return name


def _instance(name):
    for i in (cli_json("database", "list-database-instances") or []):
        if i.get("name") == name:
            return i
    return None


def _credential(instance):
    cred = cli_json("database", "generate-database-credential", "--json",
                    json.dumps({"request_id": "ontology_post_deploy", "instance_names": [instance]}))
    return cred.get("token") if isinstance(cred, dict) else None


def setup_lakebase():
    """Create the app's database on the shared instance, attach the lakebase-db
    resource, and record host + SP client id for app.yml rendering."""
    import asyncio
    if not LAKEBASE_INSTANCE:
        print("  USE_LAKEBASE=true but LAKEBASE_INSTANCE_NAME is unset. Set it to an existing "
              "Lakebase instance to enable per-user history. Skipping — history disabled.")
        return
    try:
        import asyncpg  # noqa: F401
    except ImportError:
        print("  ERROR: asyncpg not installed (pip install asyncpg). Skipping Lakebase — history disabled.")
        return

    print(f"\n[Lakebase] Reusing instance '{LAKEBASE_INSTANCE}', database '{LAKEBASE_DATABASE}'")
    inst = _instance(LAKEBASE_INSTANCE)
    waited = 0
    while (not inst or inst.get("state") != "AVAILABLE") and waited < MAX_WAIT:
        print(f"  waiting for instance AVAILABLE (state={inst.get('state') if inst else 'missing'})...")
        time.sleep(15); waited += 15
        inst = _instance(LAKEBASE_INSTANCE)
    if not inst or inst.get("state") != "AVAILABLE":
        print("  ERROR: Lakebase instance not AVAILABLE — skipping (history disabled).")
        return
    host = inst["read_write_dns"]
    _LAKEBASE["host"] = host
    print(f"  host: {host}")

    me = cli_json("current-user", "me")
    user_email = me.get("userName", "") if isinstance(me, dict) else ""
    app = cli_json("apps", "get", APP_NAME)
    sp = app.get("service_principal_client_id", "") if isinstance(app, dict) else ""
    _LAKEBASE["sp_client_id"] = sp
    if not sp:
        print("  WARNING: could not resolve app service principal client id yet.")

    async def _provision():
        import asyncpg
        token = _credential(LAKEBASE_INSTANCE)
        # 1. create the database (idempotent)
        conn = await asyncpg.connect(host=host, port=5432, database="postgres",
                                     user=user_email, password=token, ssl=_lakebase_ssl())
        try:
            await conn.execute(f'CREATE DATABASE {LAKEBASE_DATABASE}')
            print(f"  created database {LAKEBASE_DATABASE}")
        except Exception as e:
            print(f"  database {LAKEBASE_DATABASE}: {'exists' if 'already exists' in str(e) else e}")
        finally:
            await conn.close()

    asyncio.run(_provision())
    _LAKEBASE["user_email"] = user_email


def attach_lakebase():
    """Attach the lakebase-db resource, grant the app SP, and restart the app.

    Runs AFTER `bundle deploy`/`run`, because `bundle deploy` reconciles the app's
    resources from databricks.yml (which intentionally omits lakebase-db) and would
    otherwise strip it. Pinned LAKEBASE_* env stays in app.yml; the resource
    provides the SP's Postgres role + network path, so we restart to reconnect.
    """
    import asyncio
    host = _LAKEBASE.get("host")
    sp = _LAKEBASE.get("sp_client_id")
    user_email = _LAKEBASE.get("user_email", "")
    if not host:
        return True
    print("\n[Lakebase] Attaching lakebase-db resource (post-publish)")
    ok = update_app([{
        "name": "lakebase-db",
        "database": {"instance_name": LAKEBASE_INSTANCE,
                     "database_name": LAKEBASE_DATABASE,
                     "permission": "CAN_CONNECT_AND_CREATE"},
    }])

    # Grant the app SP full DML on the history objects. Schema USAGE/CREATE alone
    # is NOT enough when the tables/sequences already exist owned by another role
    # (e.g. the deploying user) — the SP then gets "permission denied". Granting
    # ALL on existing tables + sequences plus default privileges covers both the
    # pre-existing case and anything created later. Run as the owning user.
    if sp:
        grants = [
            f'GRANT USAGE, CREATE ON SCHEMA public TO "{sp}"',
            f'GRANT ALL ON ALL TABLES IN SCHEMA public TO "{sp}"',
            f'GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO "{sp}"',
            f'ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO "{sp}"',
            f'ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO "{sp}"',
        ]
        async def _grant():
            import asyncpg
            token = _credential(LAKEBASE_INSTANCE)
            conn = await asyncpg.connect(host=host, port=5432, database=LAKEBASE_DATABASE,
                                         user=user_email, password=token, ssl=_lakebase_ssl())
            try:
                for g in grants:
                    await conn.execute(g)
                print(f"  granted schema + table + sequence privileges to app SP ({sp[:12]}...)")
            except Exception as e:
                print(f"  WARNING: grant skipped ({str(e)[:120]})")
            finally:
                await conn.close()
        try:
            asyncio.run(_grant())
        except Exception as e:
            print(f"  WARNING: grant step failed: {str(e)[:120]}")

    # Restart the app so its lifespan re-runs init_pool with the resource present.
    app = cli_json("apps", "get", APP_NAME)
    app_path = ""
    if isinstance(app, dict):
        app_path = (app.get("active_deployment") or {}).get("source_code_path", "")
    if app_path:
        ok = cli("apps", "deploy", APP_NAME, "--source-code-path", app_path, check=False) == 0 and ok
    else:
        print("  WARNING: could not resolve app source path to restart; the app may need a manual redeploy.")
        ok = False
    return ok


def main():
    global APP_NAME
    if not PROFILE:
        print("ERROR: DATABRICKS_PROFILE not set.")
        sys.exit(1)
    if TARGET not in VALID_TARGETS:
        print(f"ERROR: TARGET={TARGET!r} is not a known bundle target {VALID_TARGETS}.")
        sys.exit(1)

    print("=" * 60)
    print("Genie Ontology Readiness: post-deploy")
    print("=" * 60)

    APP_NAME = resolve_app_name()
    print(f"Target: {TARGET}   App: {APP_NAME}")

    if USE_LAKEBASE:
        setup_lakebase()

    print("\n[1/3] Rendering app.yml...")
    render_app_yml()

    print("\n[2/3] Granting app SP read access...")
    env = os.environ.copy()
    # Force-set (not setdefault): a stale APP_NAME already in the environment must
    # NOT win over the target-resolved name, or the child would grant the SP on the
    # wrong environment's app.
    env["APP_NAME"] = APP_NAME
    if WAREHOUSE_ID:
        env["WAREHOUSE_ID"] = WAREHOUSE_ID
    grants_rc = subprocess.run(
        [sys.executable, str(Path(__file__).parent / "setup_app_permissions.py")], env=env).returncode
    if grants_rc != 0:
        print("  WARNING: some grants failed (see above); continuing so the app is still published,"
              " but affected pillars will show 'not available'.")

    print(f"\n[3/3] Publishing the app to target '{TARGET}' (re-sync app.yml + dist, start compute, deploy)...")
    # The app name is fixed by the target in databricks.yml — do NOT pass
    # --var app_name (the variable no longer exists; passing it would fail and,
    # historically, sharing one target's state across app names is the delete
    # footgun this hardening removes). Only warehouse_id is a real per-deploy var.
    var_args = [f"--var=warehouse_id={WAREHOUSE_ID}"] if WAREHOUSE_ID else []
    deploy_rc = cli("bundle", "deploy", "-t", TARGET, *var_args, check=False)
    if deploy_rc != 0:
        print(f"\nFAILED: `bundle deploy` exited {deploy_rc}; nothing was published. "
              f"Fix the error above and re-run.")
        sys.exit(deploy_rc)
    run_rc = cli("bundle", "run", "ontology_readiness", "-t", TARGET, *var_args, check=False)

    # `bundle run` exits non-zero both when the app deployment itself fails and
    # when the app lives outside this target's Terraform state. Ask the app which
    # one it was instead of guessing at the warehouse variable.
    app_state, dep_state, dep_message, url, source_path = deployment_status(APP_NAME)
    publish_failed = run_rc != 0 or dep_state == "FAILED" or app_state == "UNAVAILABLE"
    if publish_failed:
        if dep_state:
            explain_deploy_failure(APP_NAME, dep_state, dep_message, source_path)
        else:
            print(f"\n  `bundle run` exited {run_rc} and `apps get {APP_NAME}` returned nothing. "
                  f"If the app exists outside target '{TARGET}' state, redeploy its code with "
                  f"`databricks apps deploy {APP_NAME}`.")

    # Attach Lakebase AFTER publishing so `bundle deploy` doesn't strip the resource.
    # Still done on a failed publish: the resource binding is independent of the
    # code deployment, and the operator's next `apps deploy` then starts with it.
    problems = []
    if USE_LAKEBASE and _LAKEBASE["host"]:
        if not attach_lakebase():
            problems.append("Lakebase attach/redeploy")
        # attach_lakebase() redeploys, so the live state may have changed either way.
        # A redeploy of the *old* source path can succeed after `bundle run` failed;
        # run_rc stays in the condition so that never reads as "new code published".
        app_state, dep_state, dep_message, url, source_path = deployment_status(APP_NAME)
        publish_failed = run_rc != 0 or dep_state == "FAILED" or app_state == "UNAVAILABLE"
    elif not update_app():
        problems.append("user_api_scopes (OBO)")
    if grants_rc != 0:
        problems.append("SP grants")

    print("\n" + "=" * 60)
    if publish_failed:
        print(f"RESULT: FAILED   app={APP_NAME}   app_state={app_state or '?'}   "
              f"deployment={dep_state or '?'}")
        print(f"        {dep_message}" if dep_message else "")
        print(f"URL (will 503 until a deployment succeeds): {url}" if url else "")
        sys.exit(2)
    if problems:
        print(f"RESULT: PUBLISHED WITH ERRORS ({', '.join(problems)})   app={APP_NAME}   "
              f"deployment={dep_state or '?'}")
        print(f"URL: {url}" if url else "")
        sys.exit(3)
    print(f"RESULT: OK   app={APP_NAME}   app_state={app_state or '?'}   deployment={dep_state or '?'}")
    print(f"URL: {url}" if url else "")

if __name__ == "__main__":
    main()
