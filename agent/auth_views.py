"""
agent/auth_views.py — 认证接口（JWT：access + refresh）

对齐需求：注册 / 登录 / 刷新 / 退出（黑名单）/ 当前用户。

技术选型说明：
- 认证方案用 djangorestframework-simplejwt 做 JWT（access 短 + refresh 长），
  refresh 旋转 + 旋转后旧 token 进黑名单，退出时把 refresh 加入黑名单。
- 用户模型沿用 Django 默认 User（auth_user 表已存在），email 唯一性在应用层校验
  （默认 User 的 email 字段非 unique，故在 serializer 里显式查重）。
- 密码用 Django 的 create_user（内部 make_password 哈希存储），强度走
  AUTH_PASSWORD_VALIDATORS（默认含 8 位长度校验）。
"""
from django.conf import settings
from django.contrib.auth import authenticate, get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.cache import cache

from rest_framework import serializers, status, views
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework_simplejwt.tokens import RefreshToken

User = get_user_model()


# ──────────────────────────────────────────────────────────────
# 登录失败次数限制（可选加分项，基于 Django cache）
# ──────────────────────────────────────────────────────────────
def _is_locked(identifier: str) -> bool:
    """判断该账号/标识是否已因失败过多被锁定。"""
    attempts = cache.get(f"login_attempts_{identifier}", 0)
    return attempts >= getattr(settings, "LOGIN_MAX_ATTEMPTS", 5)


def _record_failure(identifier: str) -> None:
    key = f"login_attempts_{identifier}"
    attempts = cache.get(key, 0) + 1
    cache.set(key, attempts, getattr(settings, "LOGIN_LOCKOUT_MINUTES", 15) * 60)


def _clear_failures(identifier: str) -> None:
    cache.delete(f"login_attempts_{identifier}")


def _authenticate_identifier(identifier: str, password: str):
    """支持「用户名 或 邮箱」+ 密码登录。"""
    user = authenticate(request=None, username=identifier, password=password)
    if user is not None:
        return user
    # 尝试按邮箱反查用户名
    try:
        matched = User.objects.get(email__iexact=identifier)
    except User.DoesNotExist:
        return None
    return authenticate(request=None, username=matched.username, password=password)


def _user_payload(user) -> dict:
    return {
        "id": user.id,
        "username": user.username,
        "email": user.email or "",
    }


# ──────────────────────────────────────────────────────────────
# 注册
# ──────────────────────────────────────────────────────────────
class RegisterSerializer(serializers.ModelSerializer):
    email = serializers.EmailField(required=True)
    password = serializers.CharField(write_only=True, validators=[validate_password])
    confirm_password = serializers.CharField(write_only=True)

    class Meta:
        model = User
        fields = ("id", "username", "email", "password", "confirm_password")

    def validate_username(self, value):
        if User.objects.filter(username__iexact=value).exists():
            raise serializers.ValidationError("该用户名已被使用，请换一个")
        return value

    def validate_email(self, value):
        if User.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError("该邮箱已被注册")
        return value

    def validate(self, attrs):
        if attrs.get("password") != attrs.get("confirm_password"):
            raise serializers.ValidationError({"confirm_password": "两次输入的密码不一致"})
        return attrs

    def create(self, validated_data):
        validated_data.pop("confirm_password", None)
        return User.objects.create_user(
            username=validated_data["username"],
            email=validated_data.get("email", ""),
            password=validated_data["password"],
        )


class RegisterView(views.APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = RegisterSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        user = serializer.save()
        refresh = RefreshToken.for_user(user)
        return Response(
            {
                "message": "注册成功",
                "user": _user_payload(user),
                "access": str(refresh.access_token),
                "refresh": str(refresh),
            },
            status=status.HTTP_201_CREATED,
        )


# ──────────────────────────────────────────────────────────────
# 登录
# ──────────────────────────────────────────────────────────────
class LoginView(views.APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        identifier = (request.data.get("username") or "").strip()
        password = request.data.get("password") or ""
        if not identifier or not password:
            return Response({"detail": "请输入用户名/邮箱和密码"}, status=status.HTTP_400_BAD_REQUEST)

        if _is_locked(identifier):
            return Response(
                {"detail": f"失败次数过多，请 {getattr(settings, 'LOGIN_LOCKOUT_MINUTES', 15)} 分钟后再试"},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        user = _authenticate_identifier(identifier, password)
        if user is None:
            _record_failure(identifier)
            return Response({"detail": "用户名或密码错误"}, status=status.HTTP_401_UNAUTHORIZED)

        _clear_failures(identifier)
        refresh = RefreshToken.for_user(user)
        return Response(
            {
                "message": "登录成功",
                "user": _user_payload(user),
                "access": str(refresh.access_token),
                "refresh": str(refresh),
            }
        )


# ──────────────────────────────────────────────────────────────
# 退出（把 refresh token 加入黑名单）
# ──────────────────────────────────────────────────────────────
class LogoutView(views.APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        refresh = request.data.get("refresh", "")
        if refresh:
            try:
                RefreshToken(refresh).blacklist()
            except Exception:
                pass  # token 已失效/非法也视为退出成功
        return Response({"message": "退出成功"})


# ──────────────────────────────────────────────────────────────
# 当前用户信息
# ──────────────────────────────────────────────────────────────
class MeView(views.APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(_user_payload(request.user))
