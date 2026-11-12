from dataclasses import dataclass
from typing import Optional

from rest_framework.exceptions import AuthenticationFailed, PermissionDenied
from rest_framework.permissions import BasePermission, SAFE_METHODS

from bookings.models import AdminProfile, Company, Master
from django.contrib.auth.models import User

from .tokens import IMPERSONATION_CLAIM, STAFF_SCOPE, token_matches_password

MANAGER_ROLES = ('owner', 'admin')

NO_STAFF_TOKEN_MESSAGE = 'Нужен вход в панель салона'
NO_STAFF_ACCESS_MESSAGE = 'У этого аккаунта нет доступа к панели салона'
NOT_ENOUGH_RIGHTS_MESSAGE = 'Недостаточно прав'
SELF_SCHEDULE_FORBIDDEN_MESSAGE = 'Изменить график может администратор салона'
SESSION_EXPIRED_MESSAGE = 'Сессия устарела, войдите снова'
BLOCKED_MESSAGE = 'Салон заблокирован. Напишите в поддержку TimePoint'
IMPERSONATION_FORBIDDEN_MESSAGE = 'Недоступно при входе от имени салона'


def is_impersonation(request):
    """Запрос от супер-админа, вошедшего в панель салона от имени владельца."""
    return bool(request.auth is not None and request.auth.get(IMPERSONATION_CLAIM) is not None)


@dataclass(frozen=True)
class StaffContext:
    role: str  # owner | admin | master
    company: Company
    master: Optional[Master]  # профиль мастера в этом же салоне (у owner/admin может не быть)

    @property
    def is_manager(self):
        return self.role in MANAGER_ROLES


def get_staff(user):
    """Роль пользователя в панели салона -> StaffContext | None.

    Определяется на каждый запрос (не кешируется в токене): скрытый мастер или
    отвязанный админ теряют доступ сразу, даже с действующим токеном.
    """
    if not user or not user.is_authenticated:
        return None
    ap = AdminProfile.objects.filter(user=user, company__isnull=False).select_related('company').first()
    m = Master.objects.filter(user=user, is_active=True).select_related('company').first()
    if ap:
        return StaffContext(ap.role, ap.company, m if m and m.company_id == ap.company_id else None)
    if m:
        return StaffContext('master', m.company, m)
    return None


class IsStaff(BasePermission):
    """Вход по staff-токену + у пользователя есть роль в салоне. Кладёт контекст в request.staff."""

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            # Без токена DRF отдаст 401
            return False
        if request.auth is None or request.auth.get('scope') != STAFF_SCOPE:
            raise PermissionDenied({'error': NO_STAFF_TOKEN_MESSAGE})
        if not token_matches_password(request.auth, request.user):
            # Пароль сменили или сбросили после выдачи токена → 401, фронт выходит из панели
            raise AuthenticationFailed({'error': SESSION_EXPIRED_MESSAGE})
        superuser_id = request.auth.get(IMPERSONATION_CLAIM)
        if superuser_id is not None and not User.objects.filter(
                pk=superuser_id, is_active=True, is_superuser=True).exists():
            # Супер-админа отключили — его вход в салон тоже закрывается
            raise AuthenticationFailed({'error': SESSION_EXPIRED_MESSAGE})
        staff = get_staff(request.user)
        if staff is None:
            raise PermissionDenied({'error': NO_STAFF_ACCESS_MESSAGE})
        if staff.company.status == Company.STATUS_BLOCKED:
            raise PermissionDenied({'error': BLOCKED_MESSAGE})
        request.staff = staff
        return True


class IsStaffManager(IsStaff):
    """Владелец или администратор салона."""

    def has_permission(self, request, view):
        if not super().has_permission(request, view):
            return False
        if not request.staff.is_manager:
            raise PermissionDenied({'error': NOT_ENOUGH_RIGHTS_MESSAGE})
        return True


class IsStaffManagerOrReadOnly(IsStaff):
    """Справочники: читать может любой персонал, менять — владелец или администратор."""

    def has_permission(self, request, view):
        if not super().has_permission(request, view):
            return False
        if request.method not in SAFE_METHODS and not request.staff.is_manager:
            raise PermissionDenied({'error': NOT_ENOUGH_RIGHTS_MESSAGE})
        return True


class IsStaffManagerOrSelfSchedule(IsStaff):
    """График мастера: менять может владелец, администратор или сам мастер, если ему это разрешено.

    Мастер видит в MasterViewSet только себя, так что чужой график ему недоступен (404).
    """

    def has_permission(self, request, view):
        if not super().has_permission(request, view):
            return False
        staff = request.staff
        if request.method in SAFE_METHODS or staff.is_manager:
            return True
        if staff.master is None or not staff.master.can_edit_own_schedule:
            raise PermissionDenied({'error': SELF_SCHEDULE_FORBIDDEN_MESSAGE})
        return True


class IsStaffOwner(IsStaff):
    """Только владелец салона (финансы: отчёты, касса, зарплаты)."""

    def has_permission(self, request, view):
        if not super().has_permission(request, view):
            return False
        if request.staff.role != 'owner':
            raise PermissionDenied({'error': NOT_ENOUGH_RIGHTS_MESSAGE})
        return True
