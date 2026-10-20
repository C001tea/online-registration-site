from getpass import getpass

from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from bookings.models import AdminProfile, Company, Master


class Command(BaseCommand):
    help = (
        'Создать или обновить доступ в панель салона: владелец/администратор (AdminProfile) '
        'или мастер (Master.user). Пример: create_staff --company jcnails --username owner --role owner'
    )

    def add_arguments(self, parser):
        parser.add_argument('--company', required=True, help='slug салона')
        parser.add_argument('--username', required=True, help='логин для входа в панель')
        parser.add_argument('--role', required=True, choices=['owner', 'admin', 'master'])
        parser.add_argument('--master-id', type=int, help='id мастера (обязателен для --role master)')
        parser.add_argument('--name', help='имя пользователя (показывается в панели)')
        parser.add_argument('--password', help='пароль; без него будет запрошен с клавиатуры')
        parser.add_argument(
            '--reset-password', action='store_true',
            help='сменить пароль, если пользователь уже существует',
        )

    def handle(self, *args, **options):
        username = options['username'].strip()
        role = options['role']
        master_id = options['master_id']
        if not username:
            raise CommandError('Укажите --username')

        company = Company.objects.filter(slug=options['company']).first()
        if not company:
            raise CommandError(f'Салон со slug «{options["company"]}» не найден')
        if role == 'master' and master_id is None:
            raise CommandError('Для --role master нужен --master-id')

        user = User.objects.filter(username=username).first()
        need_password = user is None or options['reset_password']
        if user is not None and not options['reset_password'] and options['password']:
            self.stdout.write(self.style.WARNING(
                'Пользователь уже существует — --password без --reset-password игнорируется'
            ))

        password = None
        if need_password:
            password = options['password'] or self.ask_password()
            try:
                validate_password(password, user or User(username=username))
            except ValidationError as exc:
                raise CommandError('Пароль не подходит: ' + ' '.join(exc.messages))

        with transaction.atomic():
            if user is None:
                user = User.objects.create_user(username=username, password=password)
                self.stdout.write(f'Создан пользователь {username}')
            else:
                self.stdout.write(f'Пользователь {username} уже существует — привязывается существующий аккаунт')
                if password:
                    user.set_password(password)
                    self.stdout.write('Пароль изменён')
            if options['name']:
                user.first_name = options['name']
            user.save()

            if role in ('owner', 'admin'):
                profile = AdminProfile.objects.filter(user=user).first()
                if profile and profile.company_id and profile.company_id != company.id:
                    raise CommandError(
                        f'{username} уже администратор другого салона ({profile.company}) — сначала отвяжите его'
                    )
                if profile is None:
                    profile = AdminProfile(user=user)
                profile.company = company
                profile.role = role
                profile.save()

            if master_id is not None:
                # Для owner/admin --master-id необязателен: владелец может работать и мастером
                self.link_master(user, company, master_id)

        self.stdout.write(self.style.SUCCESS(
            f'Готово: {username} — {role} салона «{company.name}». Вход: /admin-panel'
        ))

    def ask_password(self):
        password = getpass('Пароль: ')
        if password != getpass('Пароль ещё раз: '):
            raise CommandError('Пароли не совпадают')
        if not password:
            raise CommandError('Пустой пароль')
        return password

    def link_master(self, user, company, master_id):
        master = Master.objects.filter(pk=master_id, company=company).first()
        if not master:
            raise CommandError(f'Мастер id={master_id} не найден в салоне «{company.slug}»')
        if master.user_id and master.user_id != user.id:
            raise CommandError(f'У мастера «{master.name}» уже другой пользователь ({master.user.username})')
        other = Master.objects.filter(user=user).exclude(pk=master.pk).first()
        if other:
            raise CommandError(f'{user.username} уже привязан к другому мастеру ({other.name}, id={other.id})')
        master.user = user
        master.save(update_fields=['user'])
        if not master.is_active:
            self.stdout.write(self.style.WARNING(
                f'Мастер «{master.name}» скрыт — войти в панель как мастер он не сможет, пока его не вернут'
            ))
