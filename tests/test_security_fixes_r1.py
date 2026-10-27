"""第一轮协同演练的修复回归测试。

覆盖三处本轮修复：
1. 会话归属默认 strict（无归属不再放行）
2. /healthz 异常不回显原文
3. /test-ai/ 异常不回显原文

只使用替身，不连接真实业务库，不发起网络请求。
"""

import os
import sys
from types import SimpleNamespace
from unittest.mock import patch

import django
from django.test import RequestFactory

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from agent import views  # noqa: E402
from config import urls as config_urls  # noqa: E402


def _get(path="/"):
    return RequestFactory().get(path)


# ── 1. 会话归属默认 strict ─────────────────────────────────────────


def test_session_error_passes_strict_flag_to_ownership_check():
    """_session_error 必须显式传 allow_unowned=False。

    修复前默认 True，导致「有 Conversations 但无带 user_id 的 AgentRun」的会话
    对任意已登录用户开放；history / feedback / handoff 三处调用都没传参。
    """
    user = SimpleNamespace(id=7, is_authenticated=True)

    with patch.object(views, "_session_belongs_to_user", return_value=False) as belongs:
        resp = views._session_error("abc123", user)

    assert resp is not None
    assert resp.status_code == 403
    belongs.assert_called_once_with("abc123", user, allow_unowned=False)


def test_session_error_allows_verified_owner():
    """归属校验通过时不拦截。"""
    user = SimpleNamespace(id=7, is_authenticated=True)

    with patch.object(views, "_session_belongs_to_user", return_value=True):
        assert views._session_error("abc123", user) is None


def test_unowned_session_is_rejected_by_default():
    """AgentRun 查不到归属时返回 False，不再被默认放行。"""
    user = SimpleNamespace(id=7, is_authenticated=True)

    with patch("agent.models.AgentRun.objects.filter") as filt:
        filt.return_value.exclude.return_value.values_list.return_value.distinct.return_value = []
        assert views._session_belongs_to_user("abc123", user) is False


# ── 2. /healthz 不回显异常原文 ─────────────────────────────────────


def test_healthz_does_not_echo_database_exception():
    """数据库异常时只返回状态标识，不把驱动原文递给匿名调用方。"""
    leaked = "Access denied for user 'root'@'10.0.0.5' (using password: YES)"

    with patch.object(config_urls, "connection") as conn:
        conn.ensure_connection.side_effect = Exception(leaked)
        resp = config_urls.healthz(_get("/healthz"))

    body = resp.content.decode()
    assert "Access denied" not in body
    assert "10.0.0.5" not in body
    assert '"error"' in body
    assert resp.status_code == 503


# ── 3. /test-ai/ 不回显上游异常 ────────────────────────────────────


def test_test_ai_does_not_echo_upstream_exception():
    """上游失败时不把异常文本（含密钥片段、上游地址）回给调用方。"""
    leaked = "401 Invalid key sk-secret-abc at https://api.deepseek.com"

    with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "sk-secret-abc"}), patch(
        "openai.OpenAI"
    ) as mock_client:
        mock_client.return_value.chat.completions.create.side_effect = Exception(leaked)
        resp = config_urls.test_ai(_get("/test-ai/"))

    body = resp.content.decode()
    assert "sk-secret-abc" not in body
    assert "api.deepseek.com" not in body
    assert "upstream_unavailable" in body
    assert resp.status_code == 500
