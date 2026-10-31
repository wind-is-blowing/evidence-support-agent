REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO kefu_exec, kefu_read, kefu_work, kefu_import;
GRANT SELECT ON products, dealers, documents, document_scopes, chunks, approved_steps TO kefu_read, kefu_work, kefu_exec;
GRANT SELECT, INSERT, UPDATE, DELETE ON products, dealers, documents, document_scopes, chunks, approved_steps TO kefu_import;
GRANT SELECT, INSERT, UPDATE, DELETE ON principals, sessions, devices, purchases, tasks, task_work, events, requests, actions, jobs, model_calls, budget_buckets, artifacts, memories, memory_exclusions, source_states, tickets TO kefu_exec;
GRANT SELECT ON tasks, task_work, events, devices, purchases, artifacts, memories, memory_exclusions, source_states, tickets TO kefu_read, kefu_work;
-- task_work contains both model projections and authenticated UI facts.
-- Writes therefore go through the executor's lease/version checked checkpoint.
-- A tool's work/read connection must never be able to forge those facts.
REVOKE INSERT, UPDATE, DELETE ON task_work FROM kefu_work;
GRANT SELECT ON alembic_version TO kefu_exec;

CREATE FUNCTION kefu_visible_task(target text) RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = public, pg_temp AS $$
 SELECT EXISTS (SELECT 1 FROM tasks WHERE id = target AND (owner_id = current_setting('app.principal_id',true) OR assigned_staff_id = current_setting('app.principal_id',true)))
$$;
REVOKE ALL ON FUNCTION kefu_visible_task(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION kefu_visible_task(text) TO kefu_exec,kefu_read,kefu_work;

ALTER TABLE tasks ENABLE ROW LEVEL SECURITY;
CREATE POLICY tasks_scope ON tasks USING (owner_id = current_setting('app.principal_id',true) OR assigned_staff_id = current_setting('app.principal_id',true))
WITH CHECK (owner_id = current_setting('app.principal_id',true) OR assigned_staff_id = current_setting('app.principal_id',true));
DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['task_work','events','requests','actions','jobs','model_calls','tickets'] LOOP
  EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY',t);
  EXECUTE format('CREATE POLICY task_scope ON %I USING (kefu_visible_task(task_id)) WITH CHECK (kefu_visible_task(task_id))',t);
 END LOOP;
 FOREACH t IN ARRAY ARRAY['devices','purchases','memories','memory_exclusions','source_states'] LOOP
  EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY',t);
  EXECUTE format('CREATE POLICY owner_scope ON %I USING (owner_id = current_setting(''app.principal_id'',true)) WITH CHECK (owner_id = current_setting(''app.principal_id'',true))',t);
 END LOOP;
END $$;
ALTER TABLE artifacts ENABLE ROW LEVEL SECURITY;
CREATE POLICY artifact_scope ON artifacts
USING (kefu_visible_task(task_id) AND owner_id = (SELECT owner_id FROM tasks WHERE id = artifacts.task_id))
WITH CHECK (kefu_visible_task(task_id) AND owner_id = (SELECT owner_id FROM tasks WHERE id = artifacts.task_id));

CREATE FUNCTION kefu_claim_job(worker text, duration integer) RETURNS TABLE(job_id text,task_id text,owner_id text,kind text,input_version integer,lease_epoch integer,target_id text)
LANGUAGE plpgsql SECURITY DEFINER SET search_path=public,pg_temp AS $$
DECLARE picked jobs%ROWTYPE; current_task tasks%ROWTYPE;
BEGIN
 SELECT j.* INTO picked FROM jobs j JOIN tasks t ON t.id=j.task_id
 WHERE j.available_at <= now() AND (j.status='queued' OR (j.status='running' AND j.lease_until < now()))
 AND (t.lease_until IS NULL OR t.lease_until < now())
 ORDER BY CASE WHEN j.kind='run_turn' THEN 0 ELSE 1 END, j.available_at
 FOR UPDATE OF j,t SKIP LOCKED LIMIT 1;
 IF NOT FOUND THEN RETURN; END IF;
 UPDATE tasks t SET lease_epoch=t.lease_epoch+1,lease_owner=worker,lease_until=now()+make_interval(secs=>duration)
 WHERE t.id=picked.task_id RETURNING * INTO current_task;
 UPDATE jobs j SET status='running',lease_owner=worker,lease_until=current_task.lease_until,attempts=j.attempts+1 WHERE j.id=picked.id;
 RETURN QUERY SELECT picked.id::text,picked.task_id::text,current_task.owner_id::text,picked.kind::text,picked.input_version,current_task.lease_epoch,picked.target_id::text;
END $$;
REVOKE ALL ON FUNCTION kefu_claim_job(text,integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION kefu_claim_job(text,integer) TO kefu_exec;

CREATE FUNCTION kefu_adopt(guest text, account text, selected text[]) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=public,pg_temp AS $$
BEGIN
 IF account IS DISTINCT FROM NULLIF(current_setting('app.principal_id',true), '') OR NOT EXISTS(SELECT 1 FROM principals WHERE id=account AND role='consumer')
 OR NOT EXISTS(SELECT 1 FROM principals WHERE id=guest AND role='guest') THEN RAISE EXCEPTION 'invalid adoption'; END IF;
 IF EXISTS(SELECT 1 FROM unnest(selected) target WHERE NOT EXISTS(SELECT 1 FROM tasks WHERE id=target AND owner_id=guest)) THEN RAISE EXCEPTION 'invalid task'; END IF;
 UPDATE tasks SET owner_id=account,input_version=input_version+1,revision=revision+1,lease_epoch=lease_epoch+1,lease_until=NULL WHERE owner_id=guest AND id=ANY(selected);
 UPDATE artifacts SET owner_id=account WHERE owner_id=guest AND task_id=ANY(selected);
 UPDATE devices SET owner_id=account WHERE owner_id=guest AND id IN (SELECT data->>'device_id' FROM task_work WHERE task_id=ANY(selected));
 -- Source versions follow selected events so future memory extraction can
 -- verify their provenance under the new owner; original event actors stay.
 INSERT INTO source_states(owner_id,source_id,version,excluded)
 SELECT account,ss.source_id,ss.version,ss.excluded FROM source_states ss
 WHERE ss.owner_id=guest AND EXISTS(SELECT 1 FROM events e WHERE e.id=ss.source_id AND e.task_id=ANY(selected))
 ON CONFLICT(owner_id,source_id) DO UPDATE
 SET version=GREATEST(source_states.version,EXCLUDED.version),excluded=source_states.excluded OR EXCLUDED.excluded;
 DELETE FROM source_states ss WHERE ss.owner_id=guest
 AND EXISTS(SELECT 1 FROM events e WHERE e.id=ss.source_id AND e.task_id=ANY(selected));
 UPDATE sessions SET revoked=true WHERE principal_id=guest;
END $$;
REVOKE ALL ON FUNCTION kefu_adopt(text,text,text[]) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION kefu_adopt(text,text,text[]) TO kefu_exec;
