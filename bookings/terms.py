"""Цена и длительность услуги у конкретного мастера (MasterServiceTerms поверх услуги)."""
from decimal import Decimal

from .models import Master, MasterServiceTerms


def terms(master, service):
    """(длительность в минутах, цена) услуги у мастера; без мастера — как у услуги."""
    row = None
    if master is not None and service is not None:
        row = MasterServiceTerms.objects.filter(master=master, service=service).first()
    duration = row.duration if row and row.duration else service.duration
    price = row.price if row and row.price is not None else service.price
    return duration, price


def duration_of(master, service):
    return terms(master, service)[0]


def price_of(appointment):
    """Цена визита: сохранённая при записи, иначе — текущая цена услуги."""
    if appointment.price is not None:
        return appointment.price
    return appointment.service.price if appointment.service else Decimal('0')


def service_ranges(services):
    """{service_id: {price_min, price_max, duration_min, duration_max}} по активным мастерам услуги."""
    services = list(services)
    ids = [s.id for s in services]
    overrides = {(t.master_id, t.service_id): t for t in MasterServiceTerms.objects.filter(service_id__in=ids)}
    links = Master.services.through.objects.filter(
        service_id__in=ids, master__is_active=True).values_list('master_id', 'service_id')
    by_service = {}
    for master_id, service_id in links:
        by_service.setdefault(service_id, []).append(master_id)
    result = {}
    for service in services:
        prices, durations = [], []
        for master_id in by_service.get(service.id, []) or [None]:
            row = overrides.get((master_id, service.id))
            prices.append(row.price if row and row.price is not None else service.price)
            durations.append(row.duration if row and row.duration else service.duration)
        result[service.id] = {
            'price_min': str(min(prices)), 'price_max': str(max(prices)),
            'duration_min': min(durations), 'duration_max': max(durations),
        }
    return result
