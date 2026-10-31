"""Claim orphaned model reservations after normal jobs without reopening tasks."""

from alembic import op

revision = "0004"
down_revision = "0003"


def upgrade():
    op.execute("""CREATE INDEX model_calls_reserved_task_created_idx
    ON model_calls(task_id,created_at,id) WHERE status='reserved'""")
    op.execute("""CREATE OR REPLACE FUNCTION kefu_claim_job(worker text, duration integer)
    RETURNS TABLE(job_id text,task_id text,owner_id text,kind text,input_version integer,lease_epoch integer,target_id text)
    LANGUAGE plpgsql SECURITY DEFINER SET search_path=public,pg_temp AS $$
    DECLARE picked jobs%ROWTYPE; current_task tasks%ROWTYPE;
    BEGIN
      IF worker IS NULL OR btrim(worker)='' OR duration IS NULL OR duration<=0 THEN
        RAISE EXCEPTION 'worker and positive lease duration required' USING ERRCODE='22023';
      END IF;
      IF NOT EXISTS (SELECT 1 FROM system_control WHERE id=1 AND NOT maintenance AND NOT recovery_required) THEN RETURN; END IF;
      SELECT j.* INTO picked FROM jobs j JOIN tasks t ON t.id=j.task_id
      WHERE j.available_at <= now() AND (j.status='queued' OR (j.status='running' AND j.lease_until < now()))
      AND (t.lease_until IS NULL OR t.lease_until < now())
      ORDER BY CASE WHEN j.kind='run_turn' THEN 0 WHEN j.kind='reconcile_budget' THEN 2 ELSE 1 END,j.available_at,j.id
      FOR UPDATE OF j,t SKIP LOCKED LIMIT 1;
      IF NOT FOUND THEN
        SELECT t.* INTO current_task FROM tasks t
        JOIN model_calls mc ON mc.task_id=t.id AND mc.status='reserved'
        WHERE t.lease_until IS NULL OR t.lease_until < now()
        ORDER BY mc.created_at,t.id,mc.id
        FOR UPDATE OF t SKIP LOCKED LIMIT 1;
        IF NOT FOUND THEN RETURN; END IF;
        INSERT INTO jobs(id,task_id,kind,input_version,target_id,status,available_at)
        VALUES('budget-reconcile:'||md5(current_task.id||':'||current_task.input_version::text),
          current_task.id,'reconcile_budget',current_task.input_version,
          'model-budget:'||md5(current_task.id),'queued',now())
        ON CONFLICT ON CONSTRAINT jobs_kind_target_id_input_version_key DO UPDATE
        SET status='queued',available_at=now(),lease_owner=NULL,lease_until=NULL
        RETURNING * INTO picked;
      END IF;
      UPDATE tasks t SET lease_epoch=t.lease_epoch+1,lease_owner=worker,lease_until=now()+make_interval(secs=>duration)
      WHERE t.id=picked.task_id RETURNING * INTO current_task;
      UPDATE jobs j SET status='running',lease_owner=worker,lease_until=current_task.lease_until,attempts=j.attempts+1 WHERE j.id=picked.id;
      RETURN QUERY SELECT picked.id::text,picked.task_id::text,current_task.owner_id::text,picked.kind::text,picked.input_version,current_task.lease_epoch,picked.target_id::text;
    END $$""")
    op.execute("REVOKE ALL ON FUNCTION kefu_claim_job(text,integer) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION kefu_claim_job(text,integer) TO kefu_exec")


def downgrade():
    raise RuntimeError("Use a verified backup for rollback")
