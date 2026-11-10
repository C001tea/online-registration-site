"""Отметка фоновой работы для супер-админки (раздел «Техническое состояние»).

  manage.py health_mark backup --detail size=12M
  manage.py health_mark restore_check --fail
Вызывается из workflow Backup и Backup restore check по SSH.
"""
from django.core.management.base import BaseCommand, CommandError

from superadmin.models import mark

KEYS = ('backup', 'restore_check')


class Command(BaseCommand):
    help = 'Отметить результат фоновой работы (backup, restore_check)'

    def add_arguments(self, parser):
        parser.add_argument('key', choices=KEYS)
        parser.add_argument('--fail', action='store_true')
        parser.add_argument('--detail', action='append', default=[], help='ключ=значение')

    def handle(self, *args, **options):
        details = {}
        for item in options['detail']:
            name, sep, value = item.partition('=')
            if not sep or not name.isidentifier():
                raise CommandError(f'Ожидается ключ=значение: {item}')
            details[name] = value[:200]
        mark(options['key'], ok=not options['fail'], **details)
        self.stdout.write(f'{options["key"]}: {"сбой" if options["fail"] else "ok"}')
