from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app import subscriptions as subs


def user(plan=None, expires=None, telegram_id=1):
    return SimpleNamespace(plan=plan, plan_expires_at=expires, telegram_id=telegram_id)


def test_effective_plan_respects_expiry_and_grace():
    now = datetime.now(timezone.utc)
    assert subs.effective_plan(user()) == "free"
    assert subs.effective_plan(user("pro", now + timedelta(days=3))) == "pro"
    assert subs.effective_plan(user("pro", now - timedelta(hours=1))) == "pro"  # grace period
    assert subs.effective_plan(user("premium", now - timedelta(days=2))) == "free"


def test_payload_parsing():
    assert subs.parse_payload("sub:pro:42") == ("pro", 42)
    assert subs.parse_payload("sub:gold:42") is None
    assert subs.parse_payload("sub:pro:x") is None
    assert subs.parse_payload("") is None


def test_premium_lifts_what_pro_does_not():
    assert subs.plan_needed("goals") == "pro"
    assert subs.plan_needed("positions") == "premium"
    assert subs.LIMITS["premium"]["positions"] is None
    assert subs.PLANS["pro"]["stars"] < subs.PLANS["premium"]["stars"]
