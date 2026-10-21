"""Explicit installer/verifier for the narrow PostgreSQL runtime API.

Only a privileged migration connection calls install_api(). Runtime has EXECUTE
on lock_workspace/save_workspace, never table privileges. Queue semantics stay
in Python: a valid application key PLUS the runtime DSN can still expose/alter
queue content. This boundary specifically protects authentication state and the
structural integrity of receipts against arbitrary runtime SQL.
"""
from __future__ import annotations

import hashlib
import re

from ops import team_review as team


def _identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", value):
        raise ValueError("invalid database identifier")
    return '"' + value + '"'


def owner_role(schema):
    _identifier(schema)
    return schema + "_api_owner" if len(schema) <= 53 else "team_api_owner_" + hashlib.sha256(schema.encode()).hexdigest()[:32]


# Each helper has a fixed path and is owned by the NOLOGIN API role. None is
# executable by PUBLIC or runtime. Schema-qualified calls avoid shadow objects.
_STAMP = r"""
CREATE OR REPLACE FUNCTION @S@._api_stamp(value text) RETURNS timestamptz
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE
SET search_path = pg_catalog, pg_temp SET TimeZone = 'UTC'
AS $fn$
BEGIN
  IF value IS NULL OR length(value) > 40 OR value !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T.*(Z|[+-][0-9]{2}:[0-9]{2})$' THEN
    RAISE EXCEPTION USING ERRCODE = 'PT422', MESSAGE = 'invalid timestamp';
  END IF;
  RETURN value::timestamptz;
EXCEPTION WHEN invalid_datetime_format OR datetime_field_overflow THEN
  RAISE EXCEPTION USING ERRCODE = 'PT422', MESSAGE = 'invalid timestamp';
END $fn$;
"""

_STATE = r"""
CREATE OR REPLACE FUNCTION @S@._api_state(rev bigint, doc jsonb) RETURNS void
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE
SET search_path = pg_catalog, pg_temp SET TimeZone = 'UTC'
AS $fn$
DECLARE entry record;
BEGIN
  IF rev IS NULL OR rev < 0 OR jsonb_typeof(doc) IS DISTINCT FROM 'object'
     OR doc - ARRAY['schema_version','revision','queue','accounts','keys','receipts'] <> '{}'::jsonb
     OR doc->'schema_version' IS DISTINCT FROM '1'::jsonb
     OR doc->'revision' IS DISTINCT FROM to_jsonb(rev)
     OR jsonb_typeof(doc->'queue') IS DISTINCT FROM 'string'
     OR jsonb_typeof(doc->'accounts') IS DISTINCT FROM 'object'
     OR jsonb_typeof(doc->'keys') IS DISTINCT FROM 'object'
     OR jsonb_typeof(doc->'receipts') IS DISTINCT FROM 'object'
     OR doc#>>'{accounts,acct_owner,role}' IS DISTINCT FROM 'owner' THEN
    RAISE EXCEPTION USING ERRCODE = 'PT500', MESSAGE = 'invalid workspace';
  END IF;
  FOR entry IN SELECT key, value FROM jsonb_each(doc->'accounts') LOOP
    IF jsonb_typeof(entry.value) IS DISTINCT FROM 'object'
       OR entry.value - ARRAY['id','name','role','active','created_at'] <> '{}'::jsonb
       OR entry.value->>'id' IS DISTINCT FROM entry.key
       OR jsonb_typeof(entry.value->'name') IS DISTINCT FROM 'string'
       OR jsonb_typeof(entry.value->'active') IS DISTINCT FROM 'boolean'
       OR (entry.value->>'role') IS NULL OR (entry.value->>'role') NOT IN ('owner','reviewer','adjudicator')
       OR (entry.key <> 'acct_owner' AND (entry.key !~ '^acct_[0-9a-f]{32}$' OR entry.value->>'role' = 'owner')) THEN
      RAISE EXCEPTION USING ERRCODE = 'PT500', MESSAGE = 'invalid workspace account';
    END IF;
    PERFORM @S@._api_stamp(entry.value->>'created_at');
  END LOOP;
  FOR entry IN SELECT key, value FROM jsonb_each(doc->'keys') LOOP
    IF entry.key !~ '^[0-9a-f]{64}$' OR jsonb_typeof(entry.value) IS DISTINCT FROM 'object'
       OR entry.value - ARRAY['account_id','expires_at'] <> '{}'::jsonb
       OR jsonb_typeof(entry.value->'account_id') IS DISTINCT FROM 'string'
       OR NOT (doc->'accounts' ? (entry.value->>'account_id')) THEN
      RAISE EXCEPTION USING ERRCODE = 'PT500', MESSAGE = 'invalid workspace key';
    END IF;
    PERFORM @S@._api_stamp(entry.value->>'expires_at');
  END LOOP;
END $fn$;
"""

_COMMAND = r"""
CREATE OR REPLACE FUNCTION @S@._api_command(rid uuid, command_text text) RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE
SET search_path = pg_catalog, pg_temp SET TimeZone = 'UTC'
AS $fn$
DECLARE cmd jsonb; action text; payload jsonb; allowed text[]; required text[]; entry record; expected text;
BEGIN
  IF rid IS NULL OR command_text IS NULL OR octet_length(convert_to(command_text,'UTF8')) > @MAX_COMMAND@ THEN
    RAISE EXCEPTION USING ERRCODE = 'PT422', MESSAGE = 'invalid command';
  END IF;
  BEGIN cmd := command_text::jsonb;
  EXCEPTION WHEN invalid_text_representation THEN
    RAISE EXCEPTION USING ERRCODE = 'PT422', MESSAGE = 'invalid command';
  END;
  IF jsonb_typeof(cmd) IS DISTINCT FROM 'object' OR cmd - ARRAY['action','payload'] <> '{}'::jsonb
     OR jsonb_typeof(cmd->'action') IS DISTINCT FROM 'string' OR jsonb_typeof(cmd->'payload') IS DISTINCT FROM 'object' THEN
    RAISE EXCEPTION USING ERRCODE = 'PT422', MESSAGE = 'invalid command';
  END IF;
  action := cmd->>'action'; payload := cmd->'payload';
  CASE action
    WHEN 'overview','accounts','claim','disagreements' THEN allowed := ARRAY[]::text[]; required := allowed;
    WHEN 'view','release' THEN allowed := ARRAY['item_id']; required := allowed;
    WHEN 'export' THEN allowed := ARRAY['include_synthetic']; required := ARRAY[]::text[];
    WHEN 'create_account' THEN allowed := ARRAY['account_id','name','role','key_digest']; required := allowed;
    WHEN 'replace_key' THEN allowed := ARRAY['account_id','key_digest']; required := allowed;
    WHEN 'set_active' THEN allowed := ARRAY['account_id','active']; required := allowed;
    WHEN 'enqueue_ad' THEN allowed := ARRAY['dataset_id','copy','product_name','category','facts','landing_text','synthetic']; required := ARRAY['dataset_id','copy','category','synthetic'];
    WHEN 'enqueue_run' THEN allowed := ARRAY['run','landing_text','synthetic']; required := ARRAY['run','synthetic'];
    WHEN 'submit' THEN allowed := ARRAY['item_id','decision','rule_ids','cited_spans','rationale']; required := ARRAY['item_id','decision'];
    WHEN 'adjudicate' THEN allowed := ARRAY['item_id','decision','rule_ids','cited_spans','rationale']; required := ARRAY['item_id','decision','rationale'];
    ELSE RAISE EXCEPTION USING ERRCODE = 'PT422', MESSAGE = 'unknown action';
  END CASE;
  IF payload - allowed <> '{}'::jsonb OR NOT (payload ?& required) THEN
    RAISE EXCEPTION USING ERRCODE = 'PT422', MESSAGE = 'invalid command fields';
  END IF;
  FOR entry IN SELECT key, value FROM jsonb_each(payload) LOOP
    expected := CASE WHEN entry.key IN ('active','synthetic','include_synthetic') THEN 'boolean'
                     WHEN entry.key IN ('facts','rule_ids','cited_spans') THEN 'array'
                     WHEN entry.key IN ('copy','run') THEN 'object' ELSE 'string' END;
    IF jsonb_typeof(entry.value) IS DISTINCT FROM expected THEN
      RAISE EXCEPTION USING ERRCODE = 'PT422', MESSAGE = 'invalid command field type';
    END IF;
  END LOOP;
  RETURN cmd;
END $fn$;
"""

_AUTH = r"""
CREATE OR REPLACE FUNCTION @S@._api_auth(doc jsonb, access_key text, action text) RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE
SET search_path = pg_catalog, pg_temp SET TimeZone = 'UTC'
AS $fn$
DECLARE digest text; credential jsonb; actor jsonb; role_name text; permitted text[];
BEGIN
  IF access_key IS NULL OR access_key !~ '^pfk_[A-Za-z0-9_-]{43}$' THEN
    RAISE EXCEPTION USING ERRCODE = 'PT401', MESSAGE = 'access denied';
  END IF;
  digest := encode(sha256(convert_to(access_key,'UTF8')),'hex');
  credential := doc->'keys'->digest;
  actor := doc->'accounts'->(credential->>'account_id');
  IF credential IS NULL OR actor IS NULL OR actor->'active' IS DISTINCT FROM 'true'::jsonb THEN
    RAISE EXCEPTION USING ERRCODE = 'PT401', MESSAGE = 'access denied';
  END IF;
  IF @S@._api_stamp(credential->>'expires_at') <= clock_timestamp() THEN
    RAISE EXCEPTION USING ERRCODE = 'PT401', MESSAGE = 'access denied';
  END IF;
  role_name := actor->>'role';
  permitted := CASE role_name
    WHEN 'owner' THEN ARRAY['overview','accounts','create_account','replace_key','set_active','enqueue_ad','enqueue_run','export']
    WHEN 'reviewer' THEN ARRAY['overview','claim','view','release','submit']
    WHEN 'adjudicator' THEN ARRAY['overview','disagreements','adjudicate','export'] ELSE ARRAY[]::text[] END;
  IF action IS NULL OR NOT (action = ANY(permitted)) THEN
    RAISE EXCEPTION USING ERRCODE = 'PT403', MESSAGE = 'action denied';
  END IF;
  RETURN actor;
END $fn$;
"""

_AUTH_DELTA = r"""
CREATE OR REPLACE FUNCTION @S@._api_auth_delta(old_doc jsonb, new_doc jsonb, actor jsonb, cmd jsonb, stamp timestamptz) RETURNS void
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE
SET search_path = pg_catalog, pg_temp SET TimeZone = 'UTC'
AS $fn$
DECLARE action text := cmd->>'action'; p jsonb := cmd->'payload'; target text := p->>'account_id';
        accounts jsonb := old_doc->'accounts'; keys jsonb := old_doc->'keys'; account jsonb;
        digest text := p->>'key_digest'; issued jsonb; days integer;
BEGIN
  IF action NOT IN ('create_account','replace_key','set_active') THEN
    IF new_doc->'accounts' IS DISTINCT FROM accounts OR new_doc->'keys' IS DISTINCT FROM keys THEN
      RAISE EXCEPTION USING ERRCODE = 'PT403', MESSAGE = 'authentication state is immutable';
    END IF;
    RETURN;
  END IF;
  IF actor->>'role' IS DISTINCT FROM 'owner' THEN
    RAISE EXCEPTION USING ERRCODE = 'PT403', MESSAGE = 'owner required';
  END IF;
  IF action = 'create_account' THEN
    IF target IS NULL OR target !~ '^acct_[0-9a-f]{32}$' OR accounts ? target
       OR (p->>'role') NOT IN ('reviewer','adjudicator')
       OR length(btrim(p->>'name')) NOT BETWEEN 1 AND 40
       OR EXISTS (SELECT 1 FROM jsonb_each(accounts) a WHERE lower(a.value->>'name') = lower(p->>'name'))
       OR (SELECT count(*) FROM jsonb_object_keys(accounts)) >= @MAX_ACCOUNTS@ THEN
      RAISE EXCEPTION USING ERRCODE = 'PT422', MESSAGE = 'invalid account creation';
    END IF;
    account := new_doc->'accounts'->target;
    IF account->>'id' IS DISTINCT FROM target OR account->>'name' IS DISTINCT FROM p->>'name'
       OR account->>'role' IS DISTINCT FROM p->>'role' OR account->'active' IS DISTINCT FROM 'true'::jsonb
       OR @S@._api_stamp(account->>'created_at') IS DISTINCT FROM stamp THEN
      RAISE EXCEPTION USING ERRCODE = 'PT422', MESSAGE = 'account differs from command';
    END IF;
    accounts := accounts || jsonb_build_object(target, account);
  ELSE
    account := accounts->target;
    IF account IS NULL THEN
      RAISE EXCEPTION USING ERRCODE = 'PT422', MESSAGE = 'account missing';
    END IF;
  END IF;
  IF action = 'set_active' THEN
    IF account->>'role' = 'owner' THEN
      RAISE EXCEPTION USING ERRCODE = 'PT403', MESSAGE = 'owner deactivation denied';
    END IF;
    accounts := jsonb_set(accounts, ARRAY[target,'active'], p->'active', false);
  ELSE
    IF digest IS NULL OR digest !~ '^[0-9a-f]{64}$' OR keys ? digest THEN
      RAISE EXCEPTION USING ERRCODE = 'PT422', MESSAGE = 'invalid new key';
    END IF;
    days := CASE WHEN account->>'role' = 'owner' THEN 90 ELSE 30 END;
    issued := new_doc->'keys'->digest;
    IF issued->>'account_id' IS DISTINCT FROM target
       OR @S@._api_stamp(issued->>'expires_at') IS DISTINCT FROM stamp + make_interval(days => days) THEN
      RAISE EXCEPTION USING ERRCODE = 'PT422', MESSAGE = 'key differs from command';
    END IF;
    SELECT coalesce(jsonb_object_agg(e.key,e.value),'{}'::jsonb) INTO keys
      FROM jsonb_each(keys) e WHERE e.value->>'account_id' <> target;
    keys := keys || jsonb_build_object(digest, issued);
  END IF;
  IF new_doc->'accounts' IS DISTINCT FROM accounts OR new_doc->'keys' IS DISTINCT FROM keys THEN
    RAISE EXCEPTION USING ERRCODE = 'PT403', MESSAGE = 'authentication delta denied';
  END IF;
END $fn$;
"""

_LOCK = r"""
CREATE OR REPLACE FUNCTION @S@.lock_workspace(access_key text, request_id uuid, command_text text)
RETURNS TABLE(revision bigint, document jsonb, db_now timestamptz)
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp SET TimeZone = 'UTC'
AS $fn$
DECLARE cmd jsonb;
BEGIN
  cmd := @S@._api_command(request_id,command_text);
  SELECT w.revision,w.document INTO revision,document FROM @S@.workspace w WHERE w.singleton = true FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE = 'PT500', MESSAGE = 'workspace missing'; END IF;
  PERFORM @S@._api_state(revision,document);
  PERFORM @S@._api_auth(document,access_key,cmd->>'action');
  db_now := clock_timestamp();
  RETURN NEXT;
END $fn$;
"""

_SAVE = r"""
CREATE OR REPLACE FUNCTION @S@.save_workspace(access_key text, request_id uuid, command_text text, expected_revision bigint, updated_document jsonb)
RETURNS bigint
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp SET TimeZone = 'UTC'
AS $fn$
DECLARE old_revision bigint; old_doc jsonb; cmd jsonb; actor jsonb; action text; receipt_id text;
        fingerprint text; previous jsonb; receipt jsonb; stamp timestamptz; is_read boolean; is_security boolean;
BEGIN
  cmd := @S@._api_command(request_id,command_text); action := cmd->>'action';
  SELECT w.revision,w.document INTO old_revision,old_doc FROM @S@.workspace w WHERE w.singleton = true FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE = 'PT500', MESSAGE = 'workspace missing'; END IF;
  PERFORM @S@._api_state(old_revision,old_doc);
  actor := @S@._api_auth(old_doc,access_key,action);
  IF expected_revision IS NULL OR expected_revision IS DISTINCT FROM old_revision THEN
    RAISE EXCEPTION USING ERRCODE = 'PT409', MESSAGE = 'revision conflict';
  END IF;
  fingerprint := encode(sha256(convert_to(command_text,'UTF8')),'hex');
  receipt_id := (actor->>'id') || ':' || request_id::text;
  previous := old_doc->'receipts'->receipt_id;
  IF previous IS NOT NULL THEN
    IF previous->>'fingerprint' IS DISTINCT FROM fingerprint THEN
      RAISE EXCEPTION USING ERRCODE = 'PT409', MESSAGE = 'request conflict';
    END IF;
    IF updated_document IS DISTINCT FROM old_doc THEN
      RAISE EXCEPTION USING ERRCODE = 'PT422', MESSAGE = 'replay cannot modify state';
    END IF;
    RETURN old_revision;
  END IF;
  is_read := action IN ('overview','view','accounts','disagreements','export');
  is_security := action IN ('replace_key','set_active') AND actor->>'role' = 'owner';
  IF updated_document IS NOT DISTINCT FROM old_doc THEN
    IF NOT is_read THEN RAISE EXCEPTION USING ERRCODE = 'PT422', MESSAGE = 'mutation requires a receipt'; END IF;
    RETURN old_revision;
  END IF;
  PERFORM @S@._api_state(old_revision + 1,updated_document);
  IF is_read THEN
    IF updated_document->'receipts' IS DISTINCT FROM old_doc->'receipts' THEN
      RAISE EXCEPTION USING ERRCODE = 'PT422', MESSAGE = 'read cannot change receipts';
    END IF;
    stamp := clock_timestamp();
  ELSE
    receipt := updated_document->'receipts'->receipt_id;
    IF jsonb_typeof(receipt) IS DISTINCT FROM 'object'
       OR receipt - ARRAY['fingerprint','action','at','result'] <> '{}'::jsonb
       OR receipt->>'fingerprint' IS DISTINCT FROM fingerprint OR receipt->>'action' IS DISTINCT FROM action
       OR jsonb_typeof(receipt->'result') IS DISTINCT FROM 'object'
       OR receipt#>'{result,revision}' IS DISTINCT FROM to_jsonb(old_revision + 1)
       OR (updated_document->'receipts' - receipt_id) IS DISTINCT FROM old_doc->'receipts' THEN
      RAISE EXCEPTION USING ERRCODE = 'PT422', MESSAGE = 'receipt delta denied';
    END IF;
    stamp := @S@._api_stamp(receipt->>'at');
    IF stamp < transaction_timestamp() OR stamp > clock_timestamp() THEN
      RAISE EXCEPTION USING ERRCODE = 'PT422', MESSAGE = 'receipt timestamp outside transaction';
    END IF;
    IF action IN ('create_account','replace_key','set_active') THEN
      IF receipt#>>'{result,account_id}' IS DISTINCT FROM cmd#>>'{payload,account_id}' THEN
        RAISE EXCEPTION USING ERRCODE = 'PT422', MESSAGE = 'receipt target differs';
      END IF;
      IF action = 'create_account' AND receipt#>'{result,account}' IS DISTINCT FROM updated_document->'accounts'->(cmd#>>'{payload,account_id}') THEN
        RAISE EXCEPTION USING ERRCODE = 'PT422', MESSAGE = 'receipt account differs';
      END IF;
      IF action = 'set_active' AND receipt#>'{result,active}' IS DISTINCT FROM cmd#>'{payload,active}' THEN
        RAISE EXCEPTION USING ERRCODE = 'PT422', MESSAGE = 'receipt active flag differs';
      END IF;
    END IF;
    IF NOT is_security AND (SELECT count(*) FROM jsonb_object_keys(old_doc->'receipts')) >= @MAX_RECEIPTS@ THEN
      RAISE EXCEPTION USING ERRCODE = 'PT507', MESSAGE = 'receipt capacity reached';
    END IF;
  END IF;
  PERFORM @S@._api_auth_delta(old_doc,updated_document,actor,cmd,stamp);
  IF NOT is_read AND NOT is_security AND octet_length(convert_to(updated_document::text,'UTF8')) > @MAX_STATE@ THEN
    RAISE EXCEPTION USING ERRCODE = 'PT507', MESSAGE = 'state capacity reached';
  END IF;
  UPDATE @S@.workspace SET revision = old_revision + 1, document = updated_document, updated_at = clock_timestamp()
    WHERE singleton = true AND revision = expected_revision;
  IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE = 'PT409', MESSAGE = 'revision conflict'; END IF;
  RETURN old_revision + 1;
END $fn$;
"""


def function_definitions(schema):
    substitutions = {"@S@": _identifier(schema), "@MAX_COMMAND@": str(team.MAX_COMMAND_BYTES),
                     "@MAX_ACCOUNTS@": str(team.MAX_ACCOUNTS), "@MAX_RECEIPTS@": str(team.MAX_RECEIPTS),
                     "@MAX_STATE@": str(team.MAX_STATE_BYTES)}
    definitions = [
        ("_api_stamp", "text", _STAMP), ("_api_state", "bigint,jsonb", _STATE),
        ("_api_command", "uuid,text", _COMMAND), ("_api_auth", "jsonb,text,text", _AUTH),
        ("_api_auth_delta", "jsonb,jsonb,jsonb,jsonb,timestamp with time zone", _AUTH_DELTA),
        ("lock_workspace", "text,uuid,text", _LOCK), ("save_workspace", "text,uuid,text,bigint,jsonb", _SAVE),
    ]
    result = []
    for name, args, text in definitions:
        for marker, value in substitutions.items():
            text = text.replace(marker, value)
        result.append((name, args, text))
    return result


def _function_signature(schema, name, args):
    return f"{_identifier(schema)}.{_identifier(name)}({args})"


def install_api(conn, *, schema="preflight_team", runtime_role=None):
    """Run inside one administrator-owned transaction; never creates runtime login.

    The caller is responsible for committing and for an explicit deployment
    authorization. Existing workspace content is untouched. Requires PG 16+,
    schema/table ownership (or sufficient grant options), and CREATEROLE with
    ADMIN on an existing API owner. A non-superuser temporarily self-grants SET
    on that owner, creates functions as the owner, then removes its own grant.
    PostgreSQL's creator ADMIN grant is preserved. Runtime never gets membership.
    """
    from psycopg import sql

    s, owner = _identifier(schema), owner_role(schema)
    o = _identifier(owner)
    current, session = conn.execute("SELECT current_user,session_user").fetchone()
    if current != session:
        raise ValueError("installation requires a direct administrator session")
    if int(conn.execute("SELECT current_setting('server_version_num')").fetchone()[0]) < 160000:
        raise ValueError("runtime API requires PostgreSQL 16 or later")
    if runtime_role is not None:
        _identifier(runtime_role)
        _validate_runtime_role(conn, runtime_role, owner)
    conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("preflight-team-api:" + schema,))
    record = conn.execute("SELECT rolcanlogin,rolsuper,rolcreaterole,rolcreatedb,rolreplication,rolbypassrls FROM pg_catalog.pg_roles WHERE rolname=%s", (owner,)).fetchone()
    if record is None:
        conn.execute(f"CREATE ROLE {o} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT")
    elif any(record):
        raise ValueError("API owner must be a restricted NOLOGIN role")
    if conn.execute("SELECT EXISTS(SELECT 1 FROM pg_catalog.pg_auth_members m JOIN pg_catalog.pg_roles r ON r.oid=m.member WHERE r.rolname=%s)", (owner,)).fetchone()[0]:
        raise ValueError("API owner cannot be a member of another role")
    temporary_set = not conn.execute("SELECT pg_catalog.pg_has_role(current_user,%s,'SET')", (owner,)).fetchone()[0]
    previous_self_grant = None
    if temporary_set:
        previous_self_grant = conn.execute("SELECT m.admin_option,m.inherit_option,m.set_option FROM pg_catalog.pg_auth_members m JOIN pg_catalog.pg_roles r ON r.oid=m.roleid WHERE r.rolname=%s AND m.member=current_user::regrole AND m.grantor=current_user::regrole", (owner,)).fetchone()
        conn.execute(f"GRANT {o} TO CURRENT_USER WITH INHERIT FALSE, SET TRUE GRANTED BY CURRENT_USER")
    conn.execute(f"REVOKE ALL ON TABLE {s}.workspace FROM PUBLIC")
    conn.execute(f"REVOKE ALL ON SCHEMA {s} FROM PUBLIC")
    conn.execute(f"GRANT USAGE, CREATE ON SCHEMA {s} TO {o}")
    conn.execute(f"GRANT SELECT, UPDATE ON TABLE {s}.workspace TO {o}")
    conn.execute(f"SET LOCAL ROLE {o}")
    for name, args, definition in function_definitions(schema):
        signature = _function_signature(schema, name, args)
        conn.execute(definition)
        conn.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        if runtime_role is not None:
            conn.execute(f"REVOKE ALL ON FUNCTION {signature} FROM {_identifier(runtime_role)}")
            if name in {"lock_workspace", "save_workspace"}:
                conn.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO {_identifier(runtime_role)}")
    conn.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(current)))
    conn.execute(f"REVOKE CREATE ON SCHEMA {s} FROM {o}")
    if temporary_set:
        if previous_self_grant is None:
            conn.execute(f"REVOKE {o} FROM CURRENT_USER GRANTED BY CURRENT_USER")
        else:
            options = ", ".join(name + " " + ("TRUE" if value else "FALSE") for name, value in zip(("ADMIN", "INHERIT", "SET"), previous_self_grant))
            conn.execute(f"GRANT {o} TO CURRENT_USER WITH {options} GRANTED BY CURRENT_USER")
    if runtime_role is not None:
        r = _identifier(runtime_role)
        conn.execute(f"REVOKE ALL ON TABLE {s}.workspace FROM {r}")
        conn.execute(f"REVOKE ALL ON SCHEMA {s} FROM {r}")
        conn.execute(f"GRANT USAGE ON SCHEMA {s} TO {r}")
        result = verify_api(conn, schema=schema, runtime_role=runtime_role)
        if not result["boundary_valid"]:
            raise ValueError("runtime API boundary verification failed")
        return result
    return {"installed": True, "runtime_granted": False, "schema": schema, "function_owner": owner}


def _validate_runtime_role(conn, role, owner):
    row = conn.execute("SELECT rolcanlogin,rolsuper,rolcreaterole,rolcreatedb,rolreplication,rolbypassrls FROM pg_catalog.pg_roles WHERE rolname=%s", (role,)).fetchone()
    if row is None or row[0] is not True or any(row[1:]) or role == owner:
        raise ValueError("runtime must be a restricted existing LOGIN role")
    # NOINHERIT does not prevent SET ROLE. Reject every membership, including
    # paths through roles that currently hold no effective inherited privileges.
    if conn.execute("SELECT EXISTS(SELECT 1 FROM pg_catalog.pg_auth_members m JOIN pg_catalog.pg_roles r ON r.oid=m.member WHERE r.rolname=%s)", (role,)).fetchone()[0]:
        raise ValueError("runtime cannot be a member of another role")


def verify_api(conn, *, schema="preflight_team", runtime_role):
    """Inspect actual effective grants and function metadata; return no credentials."""
    _identifier(schema); _identifier(runtime_role)
    owner = owner_role(schema)
    _validate_runtime_role(conn, runtime_role, owner)
    owner_info = conn.execute("SELECT rolcanlogin,rolsuper,rolcreaterole,rolcreatedb,rolreplication,rolbypassrls FROM pg_catalog.pg_roles WHERE rolname=%s", (owner,)).fetchone()
    relation = _identifier(schema) + '.workspace'
    privileges = [p for p in ("SELECT","INSERT","UPDATE","DELETE","TRUNCATE","REFERENCES","TRIGGER")
                  if conn.execute("SELECT pg_catalog.has_table_privilege(%s,%s,%s)", (runtime_role,relation,p)).fetchone()[0]]
    column_privileges = [p for p in ("SELECT","INSERT","UPDATE","REFERENCES")
                         if conn.execute("SELECT pg_catalog.has_any_column_privilege(%s,%s,%s)", (runtime_role,relation,p)).fetchone()[0]]
    schema_create = conn.execute("SELECT pg_catalog.has_schema_privilege(%s,%s,'CREATE')", (runtime_role,schema)).fetchone()[0]
    schema_usage = conn.execute("SELECT pg_catalog.has_schema_privilege(%s,%s,'USAGE')", (runtime_role,schema)).fetchone()[0]
    owner_membership = conn.execute("SELECT EXISTS(SELECT 1 FROM pg_catalog.pg_auth_members m JOIN pg_catalog.pg_roles r ON r.oid=m.member WHERE r.rolname=%s)", (owner,)).fetchone()[0]
    functions = []
    valid = bool(owner_info) and not any(owner_info) and not privileges and not column_privileges and not schema_create and schema_usage and not owner_membership
    for name, args, definition in function_definitions(schema):
        signature = _function_signature(schema,name,args)
        metadata = conn.execute("SELECT p.prosecdef,p.proconfig,r.rolname,EXISTS(SELECT 1 FROM pg_catalog.aclexplode(coalesce(p.proacl,pg_catalog.acldefault('f',p.proowner))) a WHERE a.grantee=0 AND a.privilege_type='EXECUTE'),p.prosrc,l.lanname FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_roles r ON r.oid=p.proowner JOIN pg_catalog.pg_language l ON l.oid=p.prolang WHERE p.oid=pg_catalog.to_regprocedure(%s)", (signature,)).fetchone()
        allowed = conn.execute("SELECT pg_catalog.has_function_privilege(%s,%s,'EXECUTE')", (runtime_role,signature)).fetchone()[0] if metadata else False
        public_entry = name in {"lock_workspace","save_workspace"}
        pinned = bool(metadata) and "search_path=pg_catalog, pg_temp" in (metadata[1] or [])
        source = definition.split("AS $fn$",1)[1].rsplit("$fn$;",1)[0].strip()
        definition_matches = bool(metadata) and metadata[4].strip() == source and metadata[5] == "plpgsql"
        correct = bool(metadata) and metadata[0] == public_entry and metadata[2] == owner and not metadata[3] and pinned and allowed == public_entry and definition_matches
        valid = valid and correct
        functions.append({"function": name,"valid": bool(correct),"runtime_execute": allowed,"public_execute": bool(metadata and metadata[3]),"definition_matches": definition_matches})
    logging = {name: conn.execute("SELECT pg_catalog.current_setting(%s,true)", (name,)).fetchone()[0]
               for name in ("log_statement","log_parameter_max_length","log_parameter_max_length_on_error")}
    return {"boundary_valid": bool(valid),"schema": schema,"runtime_role": runtime_role,"function_owner": owner,
            "function_owner_login": bool(owner_info and owner_info[0]),"runtime_table_privileges": privileges,
            "runtime_column_privileges": column_privileges,
            "runtime_schema_create": schema_create,"runtime_schema_usage": schema_usage,"functions": functions,"parameter_logging": logging,
            "logging_review_required": logging.get("log_parameter_max_length") != "0" or logging.get("log_parameter_max_length_on_error") != "0",
            "limitations": "A valid application key plus runtime DSN can expose the full queue; Python validates queue semantics."}
