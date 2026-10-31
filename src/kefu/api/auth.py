import asyncio
import hashlib
import secrets
import time
from collections import defaultdict, deque
from datetime import timedelta
from urllib.parse import urlsplit

import sqlalchemy as sa
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from fastapi import Request
from redis.asyncio import Redis
from redis.exceptions import RedisError

from kefu.storage import schema as s
from kefu.storage.repository import DomainError, now, uid

password_hasher = PasswordHasher()
DUMMY_HASH = password_hasher.hash(secrets.token_urlsafe(24))


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


class Auth:
    def __init__(self, db):
        self.db = db

    async def ensure_recovery_complete(self, conn):
        recovery = (
            await conn.execute(sa.text("SELECT recovery_required FROM system_control WHERE id=1"))
        ).scalar_one_or_none()
        if recovery is not False:
            raise DomainError("RECOVERY_PENDING", "服务恢复核验尚未完成，请稍后重试", 503, True)

    async def lookup(self, token):
        if not token:
            return None
        async with self.db.transaction() as conn:
            await self.ensure_recovery_complete(conn)
            return (
                (
                    await conn.execute(
                        sa.select(s.sessions, s.principals.c.role, s.principals.c.display_name)
                        .join(s.principals, s.principals.c.id == s.sessions.c.principal_id)
                        .where(
                            s.sessions.c.token_hash == digest(token),
                            s.sessions.c.revoked.is_(False),
                            s.sessions.c.expires_at > sa.func.now(),
                        )
                    )
                )
                .mappings()
                .first()
            )

    async def current(self, request: Request):
        session = await self.lookup(request.cookies.get("kefu_session"))
        if session is None:
            session = await self.lookup(request.cookies.get("kefu_guest"))
        if not session:
            raise DomainError("UNAUTHENTICATED", "请建立访客会话或重新登录", 401)
        return dict(session)

    def origin(self, request):
        configured = self.db.settings.app_origin
        allowed = {configured}
        parsed = urlsplit(configured)
        if (
            self.db.settings.environment in {"local", "test"}
            and parsed.scheme == "http"
            and parsed.hostname in {"127.0.0.1", "localhost"}
            and parsed.port is not None
        ):
            allowed.update(
                {
                    f"http://127.0.0.1:{parsed.port}",
                    f"http://localhost:{parsed.port}",
                }
            )
        if request.headers.get("origin") not in allowed:
            raise DomainError("ORIGIN_DENIED", "请求来源不被允许", 403)

    async def modifying(self, request):
        self.origin(request)
        session = await self.current(request)
        if not secrets.compare_digest(
            request.headers.get("x-csrf-token", ""), session["csrf_token"]
        ):
            raise DomainError("CSRF_DENIED", "会话校验失败，请刷新页面", 403)
        async with self.db.transaction() as conn:
            maintenance = (
                await conn.execute(
                    sa.text(
                        "SELECT maintenance OR recovery_required FROM system_control WHERE id=1"
                    )
                )
            ).scalar_one_or_none()
        if self.db.settings.maintenance or maintenance is not False:
            raise DomainError("MAINTENANCE", "服务维护中，请稍后重试", 503, True)
        return session

    async def create(self, conn, principal, response, guest=False):
        await self.ensure_recovery_complete(conn)
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        age = 8 * 3600 if principal["role"] == "staff" else 7 * 24 * 3600
        await conn.execute(
            s.sessions.insert().values(
                id=uid(),
                principal_id=principal["id"],
                token_hash=digest(token),
                csrf_token=csrf,
                expires_at=now() + timedelta(seconds=age),
            )
        )
        response.set_cookie(
            "kefu_guest" if guest else "kefu_session",
            token,
            max_age=age,
            httponly=True,
            secure=self.db.settings.secure_cookies,
            samesite="lax",
            path="/",
        )
        return {
            "principal": {k: principal[k] for k in ("id", "role", "display_name")},
            "csrf_token": csrf,
        }


class RateLimiter:
    def __init__(self, url):
        self.redis = Redis.from_url(url, socket_connect_timeout=0.3, socket_timeout=0.3)
        self.local = defaultdict(deque)
        self.lock = asyncio.Lock()

    async def check(self, category, key, limit):
        # Mirror every attempt locally so a Redis failure cannot reset this process's allowance.
        tick = time.monotonic()
        local_key = (category, key)
        async with self.lock:
            q = self.local[local_key]
            while q and q[0] <= tick - 60:
                q.popleft()
            q.append(tick)
            local_count = len(q)
            if len(self.local) > 10000:
                self.local = defaultdict(
                    deque, {k: v for k, v in self.local.items() if v and v[-1] > tick - 60}
                )
        try:
            bucket = f"kefu:rate:{category}:{digest(key)}:{int(time.time()) // 60}"
            count = await self.redis.eval(
                "local n=redis.call('INCR',KEYS[1]); if n==1 then redis.call('EXPIRE',KEYS[1],60) end; return n",
                1,
                bucket,
            )
        except RedisError:
            count = local_count
        if count > limit:
            raise DomainError("RATE_LIMIT", "请求过于频繁，请稍后再试", 429, True)

    async def notify(self):
        try:
            await self.redis.publish("kefu:wakeup", "ready")
        except RedisError:
            pass


async def verify_password(encoded, password):
    try:
        return await asyncio.to_thread(password_hasher.verify, encoded or DUMMY_HASH, password)
    except VerificationError:
        return False
