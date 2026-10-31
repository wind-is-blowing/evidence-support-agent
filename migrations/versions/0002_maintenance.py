"""Database-authoritative maintenance switch for consistent backup windows."""

from alembic import op

revision = "0002"
down_revision = "0001"


def upgrade():
    op.execute(
        "CREATE TABLE system_control (id integer PRIMARY KEY CHECK(id=1), maintenance boolean NOT NULL DEFAULT false)"
    )
    op.execute("INSERT INTO system_control(id) VALUES (1)")
    op.execute("GRANT SELECT ON system_control TO kefu_exec,kefu_read,kefu_work")
    op.execute("""CREATE OR REPLACE FUNCTION kefu_claim_job(worker text, duration integer)
    RETURNS TABLE(job_id text,task_id text,owner_id text,kind text,input_version integer,lease_epoch integer,target_id text)
    LANGUAGE plpgsql SECURITY DEFINER SET search_path=public,pg_temp AS $$
    DECLARE picked jobs%ROWTYPE; current_task tasks%ROWTYPE;
    BEGIN
      IF (SELECT maintenance FROM system_control WHERE id=1) THEN RETURN; END IF;
      SELECT j.* INTO picked FROM jobs j JOIN tasks t ON t.id=j.task_id
      WHERE j.available_at <= now() AND (j.status='queued' OR (j.status='running' AND j.lease_until < now()))
      AND (t.lease_until IS NULL OR t.lease_until < now())
      ORDER BY CASE WHEN j.kind='run_turn' THEN 0 ELSE 1 END,j.available_at
      FOR UPDATE OF j,t SKIP LOCKED LIMIT 1;
      IF NOT FOUND THEN RETURN; END IF;
      UPDATE tasks t SET lease_epoch=t.lease_epoch+1,lease_owner=worker,lease_until=now()+make_interval(secs=>duration)
      WHERE t.id=picked.task_id RETURNING * INTO current_task;
      UPDATE jobs j SET status='running',lease_owner=worker,lease_until=current_task.lease_until,attempts=j.attempts+1 WHERE j.id=picked.id;
      RETURN QUERY SELECT picked.id::text,picked.task_id::text,current_task.owner_id::text,picked.kind::text,picked.input_version,current_task.lease_epoch,picked.target_id::text;
    END $$""")


def downgrade():
    raise RuntimeError("Use a verified backup for rollback")
