import asyncio
import copy
import json
import logging
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

import sqlalchemy as sa
from fastapi import FastAPI, File, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError

from kefu.api.auth import Auth, RateLimiter, password_hasher, verify_password
from kefu.contracts import (
    AdoptTasks,
    CardRejection,
    CreateTask,
    EscalationConfirmation,
    EventInput,
    Login,
    MemoryCorrection,
    MemoryForget,
    MessageInput,
    ProductConfirmation,
    PurchaseSelection,
    Register,
    StaffUpdate,
    StepFeedback,
)
from kefu.plugins.triage import service_signals
from kefu.services import extract_explicit_urgency
from kefu.settings import Settings
from kefu.storage import schema as s
from kefu.storage.backup import MAINTENANCE_LOCK
from kefu.storage.database import Database
from kefu.storage.files import FileStore, validate_image
from kefu.storage.repository import DomainError, Repository, uid

log = logging.getLogger("kefu.api")


def require_consumer(session, authenticated=False):
    if session["role"] == "staff" or (authenticated and session["role"] != "consumer"):
        raise DomainError("FORBIDDEN", "此操作需要消费者账号", 403)


def create_app(settings=None):
    settings = settings or Settings()
    db = Database(settings)
    repo, auth = Repository(db), Auth(db)
    limiter = RateLimiter(settings.redis_url.get_secret_value())
    files = FileStore(settings.materials_root)

    @asynccontextmanager
    async def lifespan(app):
        yield
        await limiter.redis.aclose()
        await db.close()

    app = FastAPI(title="Kefu Agent", version="0.1.0", lifespan=lifespan)
    app.state.db, app.state.repo, app.state.settings = db, repo, settings

    @app.middleware("http")
    async def request_metadata(request, call_next):
        request.state.request_id = uid()
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(DomainError)
    async def domain_error(request, error):
        return JSONResponse(
            status_code=error.status,
            content={
                "code": error.code,
                "message": error.message,
                "request_id": request.state.request_id,
                "retryable": error.retryable,
                "snapshot_url": request.url.path.split("/events")[0]
                if error.status == 409
                else None,
            },
        )

    @app.exception_handler(RequestValidationError)
    @app.exception_handler(ValidationError)
    async def validation_error(request, error):
        return JSONResponse(
            status_code=422,
            content={
                "code": "INVALID_REQUEST",
                "message": "请求字段不符合接口约定",
                "request_id": request.state.request_id,
                "retryable": False,
            },
        )

    @app.exception_handler(SQLAlchemyError)
    async def database_error(request, error):
        log.error(
            "database_failure request_id=%s category=%s",
            request.state.request_id,
            type(error).__name__,
        )
        return JSONResponse(
            status_code=503,
            content={
                "code": "DATABASE_UNAVAILABLE",
                "message": "服务暂时不可用，请保留输入稍后重试",
                "request_id": request.state.request_id,
                "retryable": True,
            },
        )

    @app.get("/health/live")
    async def live():
        return {"status": "ok"}

    @app.get("/health/ready")
    async def ready():
        async with db.transaction() as conn:
            version = (
                await conn.execute(sa.text("SELECT version_num FROM alembic_version"))
            ).scalar_one()
        if version != "0004":
            raise DomainError("SCHEMA_MISMATCH", "数据库版本不匹配", 503)
        try:
            await limiter.redis.ping()
            redis_state = "ready"
        except Exception:
            redis_state = "degraded"
        return {
            "status": "ready",
            "schema": version,
            "redis": redis_state,
            "model_mode": settings.model_mode,
            "model_provider": settings.model_provider if settings.model_mode == "qwen" else None,
            "model_name": settings.model_name if settings.model_mode == "qwen" else None,
            "is_simulated": True,
        }

    @app.post("/api/session/guest")
    async def guest(request: Request, response: Response):
        auth.origin(request)
        await limiter.check("guest", request.client.host if request.client else "local", 10)
        existing = await auth.lookup(request.cookies.get("kefu_guest"))
        if existing:
            return {
                "principal": {
                    "id": existing["principal_id"],
                    "role": existing["role"],
                    "display_name": existing["display_name"],
                },
                "csrf_token": existing["csrf_token"],
            }
        principal = {"id": uid(), "role": "guest", "display_name": "访客"}
        async with db.transaction() as conn:
            await conn.execute(s.principals.insert().values(**principal))
            return await auth.create(conn, principal, response, guest=True)

    @app.get("/api/session")
    async def session(request: Request):
        current = await auth.current(request)
        guest_session = await auth.lookup(request.cookies.get("kefu_guest"))
        return {
            "principal": {
                "id": current["principal_id"],
                "role": current["role"],
                "display_name": current["display_name"],
            },
            "csrf_token": current["csrf_token"],
            "has_guest_session": guest_session is not None,
            "is_simulated": True,
            "model_mode": settings.model_mode,
        }

    @app.post("/api/session/login")
    async def login(body: Login, request: Request, response: Response):
        current = await auth.modifying(request)
        await limiter.check("login", request.client.host if request.client else "local", 5)
        async with db.transaction() as conn:
            principal = (
                (
                    await conn.execute(
                        sa.select(s.principals).where(s.principals.c.username == body.username)
                    )
                )
                .mappings()
                .first()
            )
        valid = await verify_password(
            principal["password_hash"] if principal else None, body.password
        )
        if not principal or not valid:
            raise DomainError("LOGIN_FAILED", "账号或密码错误", 401)
        async with db.transaction() as conn:
            if current["role"] != "guest":
                await conn.execute(
                    s.sessions.update().where(s.sessions.c.id == current["id"]).values(revoked=True)
                )
            return await auth.create(conn, dict(principal), response)

    @app.post("/api/session/register", status_code=201)
    async def register(body: Register, request: Request, response: Response):
        current = await auth.modifying(request)
        if current["role"] != "guest":
            raise DomainError("ALREADY_AUTHENTICATED", "请先退出当前账号，再创建新账号", 409)
        await limiter.check("register", request.client.host if request.client else "local", 5)
        encoded = await asyncio.to_thread(password_hasher.hash, body.password)
        principal = {
            "id": uid(),
            "role": "consumer",
            "display_name": body.display_name,
        }
        async with db.transaction() as conn:
            # Share the maintenance lock until account and session commit atomically.
            # The online role intentionally has no UPDATE privilege on system_control.
            unlocked = (
                await conn.execute(
                    sa.text("SELECT pg_try_advisory_xact_lock_shared(:key)"),
                    {"key": MAINTENANCE_LOCK},
                )
            ).scalar_one()
            if not unlocked:
                raise DomainError("MAINTENANCE", "服务维护中，请稍后重试", 503, True)
            # Hashing runs outside the transaction, so recheck the gate before writing.
            available = (
                await conn.execute(
                    sa.text(
                        "SELECT NOT (maintenance OR recovery_required) "
                        "FROM system_control WHERE id=1"
                    )
                )
            ).scalar_one_or_none()
            if settings.maintenance or available is not True:
                raise DomainError("MAINTENANCE", "服务维护中，请稍后重试", 503, True)
            created = (
                await conn.execute(
                    pg_insert(s.principals)
                    .values(**principal, username=body.username, password_hash=encoded)
                    .on_conflict_do_nothing(index_elements=[s.principals.c.username])
                    .returning(s.principals.c.id)
                )
            ).scalar_one_or_none()
            if created is None:
                raise DomainError("USERNAME_TAKEN", "该账号名已被使用，请换一个或前往登录", 409)
            # Keep the guest cookie: its tasks are adopted only by an explicit selection.
            return await auth.create(conn, principal, response)

    @app.post("/api/session/logout")
    async def logout(request: Request, response: Response):
        current = await auth.modifying(request)
        async with db.transaction() as conn:
            await conn.execute(
                s.sessions.update().where(s.sessions.c.id == current["id"]).values(revoked=True)
            )
        response.delete_cookie(
            "kefu_guest" if current["role"] == "guest" else "kefu_session", path="/"
        )
        return {"ok": True}

    @app.post("/api/session/adopt")
    async def adopt(body: AdoptTasks, request: Request, response: Response):
        current = await auth.modifying(request)
        require_consumer(current, True)
        guest_session = await auth.lookup(request.cookies.get("kefu_guest"))
        if not guest_session or guest_session["role"] != "guest":
            raise DomainError("NOT_FOUND", "访客会话已失效", 404)
        async with db.transaction(guest_session["principal_id"]) as conn:
            for task_id in body.task_ids:
                await repo.task(conn, task_id)
        async with db.transaction(current["principal_id"]) as conn:
            await conn.execute(
                sa.text("SELECT kefu_adopt(:guest,:account,:selected)"),
                {
                    "guest": guest_session["principal_id"],
                    "account": current["principal_id"],
                    "selected": body.task_ids,
                },
            )
            for task_id in body.task_ids:
                task = await repo.task(conn, task_id, True)
                # Transfer invalidates pre-login decisions; restart only runnable tasks.
                if task["state"] in ("READY", "RUNNING"):
                    await repo.enqueue(conn, task_id, "run_turn", task["input_version"])
        response.delete_cookie("kefu_guest", path="/")
        await limiter.notify()
        return {"ok": True, "task_ids": body.task_ids}

    @app.post("/api/tasks", status_code=201)
    async def create_task(body: CreateTask, request: Request):
        current = await auth.modifying(request)
        require_consumer(current)
        owner = current["principal_id"]
        async with db.transaction(owner) as conn:
            # Principal row serializes create_key retries before a task exists.
            await conn.execute(
                sa.select(s.principals.c.id).where(s.principals.c.id == owner).with_for_update()
            )
            old = (
                (
                    await conn.execute(
                        sa.select(s.tasks).where(
                            s.tasks.c.owner_id == owner, s.tasks.c.create_key == body.create_key
                        )
                    )
                )
                .mappings()
                .first()
            )
            if old:
                prior = await repo.task(conn, old["id"])
                if prior.get("create_body") != body.model_dump():
                    raise DomainError("IDEMPOTENCY_CONFLICT", "创建编号已用于不同内容")
                return {"id": old["id"]}
            task_id = uid()
            await conn.execute(
                s.tasks.insert().values(
                    id=task_id,
                    owner_id=owner,
                    title=body.title,
                    create_key=body.create_key,
                    state="WAITING",
                    wait_reason="user_input",
                    token_limit=settings.task_token_limit,
                )
            )
            await conn.execute(
                s.task_work.insert().values(
                    task_id=task_id,
                    data={
                        "schema_version": 1,
                        "goal": body.title,
                        "goal_version": 0,
                        "product_version": 0,
                        "region": body.region,
                        "channel": body.channel,
                        "steps": [],
                        "messages": [],
                        "create_body": body.model_dump(),
                        "is_simulated": True,
                    },
                )
            )
            await repo.emit(
                conn,
                task_id,
                owner,
                "task_state",
                {"state": "WAITING", "wait_reason": "user_input"},
            )
            return {"id": task_id}

    async def list_tasks(request, staff=False, cursor=None, limit=30):
        current = await auth.current(request)
        if staff != (current["role"] == "staff"):
            raise DomainError("FORBIDDEN", "账号入口不匹配", 403)
        query = (
            sa.select(
                s.tasks.c.id,
                s.tasks.c.title,
                s.tasks.c.state,
                s.tasks.c.wait_reason,
                s.tasks.c.updated_at,
                s.tasks.c.input_version,
            )
            .order_by(s.tasks.c.id)
            .limit(min(max(limit, 1), 100) + 1)
        )
        if cursor:
            query = query.where(s.tasks.c.id > cursor)
        async with db.transaction(current["principal_id"]) as conn:
            rows = list((await conn.execute(query)).mappings())
        return {
            "items": rows[:limit],
            "next_cursor": rows[limit - 1]["id"] if len(rows) > limit else None,
        }

    @app.get("/api/tasks")
    async def consumer_tasks(request: Request, cursor: str | None = None, limit: int = 30):
        return await list_tasks(request, cursor=cursor, limit=limit)

    @app.get("/api/staff/tasks")
    async def staff_tasks(request: Request, cursor: str | None = None, limit: int = 30):
        return await list_tasks(request, staff=True, cursor=cursor, limit=limit)

    @app.get("/api/tasks/{task_id}")
    @app.get("/api/staff/tasks/{task_id}")
    async def snapshot(task_id: str, request: Request):
        current = await auth.current(request)
        return await repo.snapshot(
            current["principal_id"], task_id, staff=current["role"] == "staff"
        )

    @app.post("/api/tasks/{task_id}/messages", status_code=202)
    async def message(task_id: str, body: MessageInput, request: Request):
        current = await auth.modifying(request)
        require_consumer(current)
        if not body.text.strip() and not body.material_ids:
            raise DomainError("EMPTY_MESSAGE", "请填写内容或上传图片", 422)
        await limiter.check("message", current["principal_id"], 10)
        owner, encoded = current["principal_id"], body.model_dump()
        async with db.transaction(owner) as conn:
            task = await repo.task(conn, task_id, True)
            repeated = await repo.repeated(conn, owner, task_id, body.client_event_id, encoded)
            if repeated:
                return repeated
            await owned_materials(conn, task_id, body.material_ids)
            event = await repo.emit(
                conn,
                task_id,
                owner,
                "message",
                {
                    "role": "user",
                    "text": body.text,
                    "material_ids": body.material_ids,
                    "timezone": body.timezone,
                },
            )
            await conn.execute(
                s.source_states.insert().values(owner_id=owner, source_id=event["id"], version=1)
            )
            task.setdefault("messages", []).append(
                {
                    "role": "user",
                    "content": body.text,
                    "text": body.text,
                    "id": event["id"],
                    "material_ids": body.material_ids,
                    "timezone": body.timezone,
                }
            )
            task["last_user_message"] = body.text
            explicit_urgency = extract_explicit_urgency(body.text)
            signals = service_signals(body.text, explicit_urgency)
            if task.get("service_signals", {}).get("safety_risk"):
                signals = {**task["service_signals"], "intent": "handoff", "acknowledgement": ""}
            task["service_signals"] = {**signals, "source_event_id": event["id"]}
            task["service_intent"] = signals["intent"]
            if signals["intent"] != "technical":
                task["current_card"] = None
            if signals["acknowledgement"]:
                await repo.emit(
                    conn,
                    task_id,
                    owner,
                    "message",
                    {
                        "role": "assistant",
                        "text": signals["acknowledgement"],
                        "evidence_refs": [],
                        "service_acknowledgement": True,
                    },
                )
            task.pop("purchase_lookup_version", None)
            task.pop("warranty_check_version", None)
            if signals["intent"] != "handoff":
                task.pop("purchase_lookup", None)
                task.pop("warranty", None)
            if explicit_urgency:
                task["urgency"] = explicit_urgency
                task["urgency_source_event_id"] = event["id"]
            task["material_ids"] = list(
                dict.fromkeys(task.get("material_ids", []) + body.material_ids)
            )
            # A new symptom or photo can change which evidence and reviewed steps apply.
            # Force one fresh retrieval for this input instead of reusing the previous turn.
            task.pop("grounding_product_version", None)
            task.pop("retrieved_steps", None)
            task.pop("retrieval_followup", None)
            if task.get("goal") == task.get("title") and len(task.get("messages", [])) == 1:
                task["goal"] = body.text or "图片售后咨询"
                task["goal_version"] = task.get("goal_version", 0) + 1
            task.pop("pending_decision", None)
            if (task.get("current_card") or {}).get("type") == "escalation":
                task["current_card"] = None
                task.pop("draft", None)
            await repo.mutate_input(conn, task)
            result = {
                "id": event["id"],
                "message_id": event["id"],
                "input_version": task["input_version"],
            }
            await repo.remember_request(conn, owner, task_id, body.client_event_id, encoded, result)
        await limiter.notify()
        return result

    async def owned_materials(conn, task_id, material_ids):
        if not material_ids:
            return
        found = list(
            (
                await conn.execute(
                    sa.select(s.artifacts.c.id).where(
                        s.artifacts.c.task_id == task_id,
                        s.artifacts.c.kind == "upload",
                        s.artifacts.c.id.in_(material_ids),
                    )
                )
            ).scalars()
        )
        if set(found) != set(material_ids):
            raise DomainError("NOT_FOUND", "材料不存在或不可访问", 404)

    @app.post("/api/tasks/{task_id}/events", status_code=202)
    async def post_event(task_id: str, body: EventInput, request: Request):
        current = await auth.modifying(request)
        require_consumer(current)
        owner, encoded = current["principal_id"], body.model_dump()
        async with db.transaction(owner) as conn:
            task = await repo.task(conn, task_id, True)
            repeated = await repo.repeated(conn, owner, task_id, body.client_event_id, encoded)
            if repeated:
                return repeated
            if task["input_version"] != body.expected_version:
                raise DomainError("STALE_VERSION", "信息已更新，请查看当前步骤")
            card, state, reason = task.get("current_card") or {}, "READY", None
            payload = body.payload
            pending = (
                await conn.execute(
                    sa.select(s.actions.c.id).where(
                        s.actions.c.task_id == task_id,
                        s.actions.c.status.in_(["confirmed", "submitting", "unknown"]),
                    )
                )
            ).first()
            if body.type == "purchase_selected":
                require_consumer(current, True)
                if pending:
                    raise DomainError(
                        "ACTION_PENDING",
                        "当前操作正在提交或核验，请完成后再切换订单",
                        409,
                    )
                selection = PurchaseSelection.model_validate(payload)
                row = (
                    (
                        await conn.execute(
                            sa.select(s.purchases).where(
                                s.purchases.c.id == selection.purchase_id,
                                s.purchases.c.owner_id == owner,
                            )
                        )
                    )
                    .mappings()
                    .first()
                )
                if not row:
                    raise DomainError("NOT_FOUND", "订单不存在或不可访问", 404)
                product = (
                    await conn.execute(
                        sa.select(s.products.c.data).where(s.products.c.id == row["product_id"])
                    )
                ).scalar_one_or_none()
                if not product:
                    raise DomainError("PRODUCT_UNAVAILABLE", "订单商品目录暂不可用", 409)
                purchase = {"id": row["id"], **row["data"]}
                task["product"] = product
                task["product_version"] = task.get("product_version", 0) + 1
                for key in (
                    "warranty",
                    "purchase_lookup",
                    "draft",
                    "retrieved_steps",
                    "current_step",
                    "device_id",
                    "evidence",
                    "evidence_refs",
                ):
                    task.pop(key, None)
                task["purchase"] = {
                    **purchase,
                    "product_version": task["product_version"],
                    "text": f"{purchase.get('order_number', row['id'])} · {purchase.get('product_name', product.get('name', '商品'))}",
                }
                task["region"] = purchase.get("region") or task.get("region")
                task["channel"] = purchase.get("channel") or task.get("channel")
                task["current_card"] = None
                state, reason = "WAITING", "purchase_selected"
            elif body.type == "product_confirmed":
                product_confirmation = ProductConfirmation.model_validate(payload)
                check_card(
                    card, "product", product_confirmation.card_id, product_confirmation.card_version
                )
                product = next(
                    (
                        x
                        for x in card.get("candidates", [])
                        if x.get("id", x.get("product_id")) == product_confirmation.product_id
                    ),
                    None,
                )
                if not product:
                    raise DomainError("INVALID_PRODUCT", "产品不属于当前候选", 422)
                if product_confirmation.device_id:
                    device = (
                        await conn.execute(
                            sa.select(s.devices).where(
                                s.devices.c.id == product_confirmation.device_id,
                                s.devices.c.owner_id == owner,
                                s.devices.c.product_id == product_confirmation.product_id,
                            )
                        )
                    ).first()
                    if not device:
                        raise DomainError("NOT_FOUND", "设备不存在或不可访问", 404)
                    task["device_id"] = product_confirmation.device_id
                else:
                    task["device_id"] = uid()
                    await conn.execute(
                        s.devices.insert().values(
                            id=task["device_id"],
                            owner_id=owner,
                            product_id=product_confirmation.product_id,
                            data={"label": product.get("name", "设备")},
                        )
                    )
                task["product"] = product
                task["product_version"] = task.get("product_version", 0) + 1
                for key in (
                    "purchase",
                    "warranty",
                    "purchase_lookup",
                    "draft",
                    "retrieved_steps",
                    "current_step",
                    "evidence",
                    "evidence_refs",
                ):
                    task.pop(key, None)
                task["current_card"] = None
            elif body.type in ("product_rejected", "product_correction"):
                if body.type == "product_rejected":
                    rejection = CardRejection.model_validate(payload)
                    check_card(card, "product", rejection.card_id, rejection.card_version)
                elif payload:
                    raise DomainError("INVALID_REQUEST", "更正产品不接受额外字段", 422)
                task["product"] = None
                task["product_version"] = task.get("product_version", 0) + 1
                for key in (
                    "purchase",
                    "warranty",
                    "purchase_lookup",
                    "draft",
                    "retrieved_steps",
                    "current_step",
                    "device_id",
                    "evidence",
                    "evidence_refs",
                ):
                    task.pop(key, None)
                task["current_card"] = None
            elif body.type == "step_feedback":
                feedback_input = StepFeedback.model_validate(payload)
                if (
                    card.get("type") != "step"
                    or card.get("step_instance_id", card.get("instance_id"))
                    != feedback_input.step_instance_id
                    or task.get("product_version") != feedback_input.product_version
                ):
                    raise DomainError("STALE_CARD", "当前操作卡已失效")
                feedback = {
                    **card,
                    "user_feedback": feedback_input.feedback,
                    "feedback": feedback_input.feedback,
                    "feedback_text": feedback_input.text,
                }
                task.setdefault("steps", []).append(feedback)
                task["current_step"] = feedback
                for old in task.get("step_history", []):
                    if old.get("step_instance_id") == feedback_input.step_instance_id:
                        old.update(
                            user_feedback=feedback_input.feedback,
                            feedback=feedback_input.feedback,
                            feedback_text=feedback_input.text,
                        )
                task["current_card"] = None
                if feedback_input.feedback != "recovered":
                    feedback_queries = {
                        "performed": "已执行前一步，需要下一步检查",
                        "no_change": "前一步无变化，需要下一步复查",
                        "unable": "前一步无法执行，需要替代检查",
                    }
                    task["retrieval_followup"] = "\n".join(
                        value
                        for value in (
                            feedback_input.text.strip(),
                            feedback_queries[feedback_input.feedback],
                        )
                        if value
                    )
                    for key in (
                        "grounding_product_version",
                        "retrieved_steps",
                        "evidence",
                        "evidence_refs",
                    ):
                        task.pop(key, None)
                if feedback_input.feedback == "recovered" and not pending:
                    state, reason = "COMPLETED", "user_verified_recovery"
            elif body.type == "escalation_confirmed":
                require_consumer(current, True)
                confirmation_input = EscalationConfirmation.model_validate(payload)
                if (
                    card.get("type") != "escalation"
                    or card.get("draft_id") != confirmation_input.draft_id
                    or card.get("draft_version", card.get("version"))
                    != confirmation_input.draft_version
                ):
                    raise DomainError("STALE_DRAFT", "升级内容已更新，请重新核对")
                if pending:
                    raise DomainError("ACTION_PENDING", "已有工单正在提交或核验")
                draft = copy.deepcopy(task.get("draft") or card)
                if (
                    draft != card
                    or draft.get("draft_id") != confirmation_input.draft_id
                    or draft.get("draft_version") != confirmation_input.draft_version
                ):
                    raise DomainError("STALE_DRAFT", "草案与已展示内容不一致，请刷新")
                import hashlib

                unsigned = {k: v for k, v in draft.items() if k != "content_hash"}
                expected_hash = hashlib.sha256(
                    json.dumps(unsigned, sort_keys=True, ensure_ascii=False).encode()
                ).hexdigest()
                if not secrets.compare_digest(draft.get("content_hash", ""), expected_hash):
                    raise DomainError("STALE_DRAFT", "草案完整性验证失败")
                if draft.get("product_version", task.get("product_version")) != task.get(
                    "product_version"
                ) or not task.get("product"):
                    raise DomainError("STALE_DRAFT", "产品已变更，请重新生成草案")
                if draft.get("recipient") != "模拟售后服务台":
                    raise DomainError("INVALID_RECIPIENT", "接收方未配置", 422)
                ids = draft.get("material_ids", draft.get("materials", []))
                ids = [x["id"] if isinstance(x, dict) else x for x in ids]
                await owned_materials(conn, task_id, ids)
                confirmation = await repo.emit(
                    conn,
                    task_id,
                    owner,
                    "confirmation",
                    {
                        "draft_id": confirmation_input.draft_id,
                        "draft_version": confirmation_input.draft_version,
                    },
                    "internal",
                )
                action_id = uid()
                await conn.execute(
                    s.actions.insert().values(
                        id=action_id,
                        task_id=task_id,
                        draft_id=confirmation_input.draft_id,
                        draft_version=confirmation_input.draft_version,
                        operation_id=uid(),
                        confirmation_event_id=confirmation["id"],
                        status="confirmed",
                        draft=draft,
                    )
                )
                await repo.enqueue(conn, task_id, "reconcile_action", 0, action_id)
                task["current_card"] = None
                state, reason = "WAITING", "submitting"
            elif body.type == "stop":
                if payload:
                    raise DomainError("INVALID_REQUEST", "停止事件不接受额外字段", 422)
                state, reason = (
                    ("PAUSED", "action_unknown") if pending else ("CANCELLED", "user_stopped")
                )
                task["stop_requested"] = True
            elif body.type == "resume":
                if payload or task["state"] not in ("PAUSED", "CANCELLED") or pending:
                    raise DomainError("CANNOT_RESUME", "当前任务不能继续，请等待核验或补充信息")
                if task["tokens_used"] + task["tokens_reserved"] >= task["token_limit"]:
                    raise DomainError("BUDGET_EXHAUSTED", "累计额度已用尽，需要维护者调整")
                task["stop_requested"] = False
                # Retrying a paused turn must re-evaluate knowledge. This also lets newly
                # reviewed knowledge become available without changing the selected product.
                task.pop("grounding_product_version", None)
                task.pop("retrieved_steps", None)
            await repo.emit(
                conn, task_id, owner, "user_event", {"event_type": body.type, **payload}, "internal"
            )
            await repo.mutate_input(conn, task, state, reason)
            result = {"ok": True, "input_version": task["input_version"]}
            await repo.remember_request(conn, owner, task_id, body.client_event_id, encoded, result)
        await limiter.notify()
        return result

    @app.post("/api/tasks/{task_id}/uploads", status_code=201)
    async def upload(task_id: str, request: Request, file: UploadFile = File(...)):
        current = await auth.modifying(request)
        require_consumer(current)
        owner = current["principal_id"]
        async with db.transaction(owner) as conn:
            await repo.task(conn, task_id)
        content = await file.read(10 * 1024 * 1024 + 1)
        mime = await asyncio.to_thread(validate_image, content)
        material_id = uid()
        async with db.transaction(owner) as conn:
            await repo.task(conn, task_id, True)
            existing = list(
                (
                    await conn.execute(
                        sa.select(s.artifacts.c.data).where(
                            s.artifacts.c.task_id == task_id, s.artifacts.c.kind == "upload"
                        )
                    )
                ).scalars()
            )
            if sum(x.get("size_bytes", 0) for x in existing) + len(content) > 50 * 1024 * 1024:
                raise DomainError("TASK_UPLOAD_LIMIT", "每个任务最多上传 50 MiB 图片", 413)
            checksum = await asyncio.to_thread(files.put, material_id, content)
            data = {
                "storage_key": material_id,
                "sha256": checksum,
                "mime_type": mime,
                "size_bytes": len(content),
                "filename": (file.filename or "image")[:200],
            }
            await conn.execute(
                s.artifacts.insert().values(
                    id=material_id, owner_id=owner, task_id=task_id, kind="upload", data=data
                )
            )
        return {
            "id": material_id,
            "url": f"/api/materials/{material_id}",
            "mime_type": mime,
            "size_bytes": len(content),
        }

    @app.get("/api/materials/{material_id}")
    async def material(material_id: str, request: Request):
        current = await auth.current(request)
        async with db.transaction(current["principal_id"]) as conn:
            row = (
                (
                    await conn.execute(
                        sa.select(s.artifacts).where(
                            s.artifacts.c.id == material_id, s.artifacts.c.kind == "upload"
                        )
                    )
                )
                .mappings()
                .first()
            )
        if not row or row["data"].get("status") in ("deleted", "tombstoned"):
            raise DomainError("NOT_FOUND", "材料不存在或不可访问", 404)
        content = await asyncio.to_thread(
            files.read, row["data"]["storage_key"], row["data"]["sha256"]
        )
        return Response(
            content, media_type=row["data"]["mime_type"], headers={"Content-Disposition": "inline"}
        )

    @app.get("/api/tasks/{task_id}/events")
    async def event_stream(task_id: str, request: Request, after: int = 0, once: bool = False):
        current = await auth.current(request)
        async with db.transaction(current["principal_id"]) as conn:
            await repo.task(conn, task_id)
        cursor = max(after, int(request.headers.get("last-event-id", "0")))

        async def stream():
            nonlocal cursor
            last_check = 0.0
            while True:
                if await request.is_disconnected():
                    return
                if time.monotonic() - last_check >= 15:
                    try:
                        renewed = await auth.current(request)
                        if renewed["id"] != current["id"]:
                            return
                    except DomainError:
                        return
                    last_check = time.monotonic()
                    yield ": heartbeat\n\n"
                async with db.transaction(current["principal_id"]) as conn:
                    try:
                        task = await repo.task(conn, task_id)
                    except DomainError:
                        return
                    rows = (
                        (
                            await conn.execute(
                                sa.select(s.events)
                                .where(s.events.c.task_id == task_id, s.events.c.seq > cursor)
                                .order_by(s.events.c.seq)
                                .limit(201)
                            )
                        )
                        .mappings()
                        .all()
                    )
                if len(rows) > 200 or cursor > task["last_event_seq"]:
                    yield "event: reset\ndata: {}\n\n"
                    return
                for row in rows:
                    cursor = row["seq"]
                    if row["visibility"] != "public" and current["role"] != "staff":
                        continue
                    if row["type"] not in {
                        "message",
                        "progress",
                        "product_card",
                        "step_card",
                        "escalation_card",
                        "receipt",
                        "memory_changed",
                        "task_state",
                    }:
                        continue
                    # Persisted events invalidate the view; snapshots project current authorization.
                    payload = {"schema_version": 1, "refresh": True}
                    wire = {
                        "seq": row["seq"],
                        "type": row["type"],
                        "payload": payload,
                        "created_at": row["created_at"].isoformat(),
                    }
                    yield f"id: {row['seq']}\nevent: {row['type']}\ndata: {json.dumps(wire, ensure_ascii=False)}\n\n"
                if once:
                    return
                await asyncio.sleep(1)

        return StreamingResponse(
            stream(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"}
        )

    @app.post("/api/staff/tasks/{task_id}/updates", status_code=202)
    async def staff_update(task_id: str, body: StaffUpdate, request: Request):
        current = await auth.modifying(request)
        if current["role"] != "staff":
            raise DomainError("FORBIDDEN", "需要人工客服账号", 403)
        actor, encoded = current["principal_id"], body.model_dump()
        async with db.transaction(actor) as conn:
            task = await repo.task(conn, task_id, True)
            previous = await repo.repeated(conn, actor, task_id, body.client_event_id, encoded)
            if previous:
                return previous
            if task["input_version"] != body.expected_version:
                raise DomainError("STALE_VERSION", "任务已更新，请刷新")
            if body.type == "resolved" and not body.evidence_refs:
                raise DomainError("EVIDENCE_REQUIRED", "核验恢复结果需要处理依据", 422)
            await repo.emit(
                conn,
                task_id,
                actor,
                "message",
                {
                    "role": "staff",
                    "text": body.text,
                    "update_type": body.type,
                    "evidence_refs": body.evidence_refs,
                },
                "internal" if body.type == "internal_note" else "public",
            )
            if body.type == "internal_note":
                # Private annotations do not reopen completed work, invalidate
                # user cards, or interrupt an in-flight model decision.
                result = {"ok": True, "input_version": task["input_version"]}
                await repo.remember_request(
                    conn, actor, task_id, body.client_event_id, encoded, result
                )
                return result
            pending = (
                await conn.execute(
                    sa.select(s.actions.c.id).where(
                        s.actions.c.task_id == task_id,
                        s.actions.c.status.in_(["confirmed", "submitting", "unknown"]),
                    )
                )
            ).first()
            state = "COMPLETED" if body.type == "resolved" and not pending else "WAITING"
            await repo.mutate_input(
                conn,
                task,
                state,
                "staff_verified_recovery" if state == "COMPLETED" else "human_service",
            )
            result = {"ok": True, "input_version": task["input_version"]}
            await repo.remember_request(conn, actor, task_id, body.client_event_id, encoded, result)
        await limiter.notify()
        return result

    @app.get("/api/me/memories")
    async def get_memories(request: Request):
        current = await auth.current(request)
        require_consumer(current, True)
        from kefu.storage.memory import PostgresMemoryRepository

        records = await PostgresMemoryRepository(db).list_memories(current["principal_id"])
        return {"items": [r for r in records if r.get("status") == "active"]}

    @app.get("/api/me/purchases")
    async def get_purchases(request: Request):
        current = await auth.current(request)
        require_consumer(current, True)
        async with db.transaction(current["principal_id"], "read") as conn:
            rows = (
                (
                    await conn.execute(
                        sa.select(s.purchases, s.products.c.data.label("product"))
                        .join(s.products, s.products.c.id == s.purchases.c.product_id)
                        .where(s.purchases.c.owner_id == current["principal_id"])
                        .order_by(
                            s.purchases.c.data["purchased_at"].astext.desc(), s.purchases.c.id
                        )
                        .limit(100)
                    )
                )
                .mappings()
                .all()
            )
        return {
            "items": [
                {
                    "id": row["id"],
                    "product_id": row["product_id"],
                    "product_name": row["data"].get("product_name")
                    or row["product"].get("name", "商品"),
                    "model": row["data"].get("model") or row["product"].get("model", ""),
                    "order_number": row["data"].get("order_number", row["id"]),
                    "purchased_at": row["data"].get("purchased_at"),
                    "status": row["data"].get("status", "unknown"),
                    "channel": row["data"].get("channel"),
                    "region": row["data"].get("region"),
                    "amount": row["data"].get("amount"),
                    "is_simulated": bool(row["data"].get("is_simulated")),
                }
                for row in rows
            ]
        }

    @app.get("/api/me/devices")
    async def get_devices(request: Request):
        current = await auth.current(request)
        require_consumer(current, True)
        async with db.transaction(current["principal_id"]) as conn:
            rows = (
                (
                    await conn.execute(
                        sa.select(s.devices).where(s.devices.c.owner_id == current["principal_id"])
                    )
                )
                .mappings()
                .all()
            )
        return {
            "items": [
                {
                    "id": row["id"],
                    "product_id": row["product_id"],
                    "label": row["data"].get("label", "我的设备"),
                }
                for row in rows
            ]
        }

    async def memory_change(memory_id, body, request, forget=False):
        current = await auth.modifying(request)
        require_consumer(current, True)
        from kefu.storage.memory import PostgresMemoryRepository

        return await PostgresMemoryRepository(db).change(
            current["principal_id"],
            memory_id,
            body.expected_version,
            None if forget else body.text,
            body.client_event_id,
        )

    @app.post("/api/me/memories/{memory_id}/corrections")
    async def correction(memory_id: str, body: MemoryCorrection, request: Request):
        return await memory_change(memory_id, body, request)

    @app.post("/api/me/memories/{memory_id}/forget")
    async def forget(memory_id: str, body: MemoryForget, request: Request):
        return await memory_change(memory_id, body, request, True)

    frontend = Path("web/dist").resolve()
    if (frontend / "index.html").is_file():
        app.mount("/assets", StaticFiles(directory=frontend / "assets"), name="frontend-assets")

        @app.get("/", include_in_schema=False)
        @app.get("/login", include_in_schema=False)
        @app.get("/tasks", include_in_schema=False)
        @app.get("/tasks/{task_id}", include_in_schema=False)
        @app.get("/staff/tasks", include_in_schema=False)
        @app.get("/staff/tasks/{task_id}", include_in_schema=False)
        @app.get("/me/memories", include_in_schema=False)
        async def frontend_page():
            return FileResponse(frontend / "index.html")

    return app


def check_card(card, kind, card_id, version):
    if (
        card.get("type") != kind
        or card.get("id", card.get("card_id")) != card_id
        or card.get("version", card.get("card_version")) != version
    ):
        raise DomainError("STALE_CARD", "信息已更新，请查看当前步骤")


app = create_app()
