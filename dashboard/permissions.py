from dataclasses import dataclass
from typing import Optional

from rest_framework.exceptions import AuthenticationFailed, PermissionDenied
from rest_framework.permissions import BasePermission, SAFE_METHODS

from bookings.models import AdminProfile, Company, Master
from .tokens import STAFF_SCOPE, token_matches_password

MANAGER_ROLES = ('owner', 'admin')

NO_STAFF_TOKEN_MESSAGE = 'Нужен вход в панель салона'
NO_STAFF_ACCESS_MESSAGE = 'У этого аккаунта нет доступа к панели салона'
NOT_ENOUGH_RIGHTS_MESSAGE = 'Недостаточно прав'
SESSION_EXPIRED_MESSAGE = 'Сессия устарела, войдите снова'


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
        staff = get_staff(request.user)
        if staff is None:
            raise PermissionDenied({'error': NO_STAFF_ACCESS_MESSAGE})
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
