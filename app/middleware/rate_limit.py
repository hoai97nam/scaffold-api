"""
Redis sliding-window rate limiter.
Checks per-minute, per-day, and per-month windows atomically.
"""
import time

from fastapi import HTTPException, status


class RateLimiter:
    """
    Sliding window rate limiter using Redis sorted sets.
    Three windows per user: per-minute, per-day, per-month.
    """

    def __init__(self, redis):
        self.redis = redis

    async def check(self, user_id: str, limits: dict) -> None:
        now = time.time()
        pipe = self.redis.pipeline()

        windows = {
            "minute": {"ttl": 60,       "limit": limits["rpm"],       "start": now - 60},
            "day":    {"ttl": 86_400,   "limit": limits["rpd"],       "start": now - 86_400},
            "month":  {"ttl": 31*86_400,"limit": limits["rpm_month"], "start": now - 31*86_400},
        }

        for window_name, cfg in windows.items():
            key = f"rl:{user_id}:{window_name}"
            pipe.zremrangebyscore(key, 0, cfg["start"])   # remove old entries
            pipe.zcard(key)                                # count remaining
            pipe.zadd(key, {f"{now}": now})               # add current request
            pipe.expire(key, cfg["ttl"])

        results = await pipe.execute()

        for i, (window_name, cfg) in enumerate(windows.items()):
            count = results[i * 4 + 1]  # zcard is index 1 per group-of-4
            if count >= cfg["limit"]:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    headers={
                        "X-RateLimit-Limit": str(cfg["limit"]),
                        "X-RateLimit-Window": window_name,
                        "Retry-After": str(cfg["ttl"]),
                    },
                    detail={
                        "code": "RATE_LIMIT_EXCEEDED",
                        "window": window_name,
                        "limit": cfg["limit"],
                        "message": f"Rate limit exceeded: {cfg['limit']} requests per {window_name}",
                    },
                )
