"""Нагрузочный тест API на чтение (запись не создаётся).

Запуск: python3 deploy/load_test.py https://api.timepoint.tech jcnails
Имитирует клиентов, которые открывают сайт и выбирают время: информация о салоне,
мастера, услуги, свободные слоты. Для каждого уровня одновременных клиентов
печатает запросов в секунду, задержки (p50/p95/p99) и долю ошибок.
"""
import asyncio
import datetime
import json
import sys
import time

import aiohttp

BASE, SLUG = sys.argv[1].rstrip('/'), sys.argv[2]
LEVELS = [int(x) for x in (sys.argv[3] if len(sys.argv) > 3 else '10,25,50,100,200,400').split(',')]
STAGE_SECONDS = int(sys.argv[4]) if len(sys.argv) > 4 else 30


async def discover(session):
    async with session.get(f'{BASE}/api/companies/{SLUG}/masters/') as r:
        masters = await r.json(content_type=None)
    async with session.get(f'{BASE}/api/companies/{SLUG}/services/') as r:
        services = await r.json(content_type=None)
    return masters[0]['id'], services[0]['id']


def percentile(values, p):
    values = sorted(values)
    return values[min(len(values) - 1, int(len(values) * p / 100))] * 1000


async def client(session, urls, stop, latencies, errors, codes):
    i = 0
    while time.monotonic() < stop:
        url = urls[i % len(urls)]
        i += 1
        start = time.monotonic()
        try:
            async with session.get(url) as r:
                await r.read()
                latencies.append(time.monotonic() - start)
                codes[r.status] = codes.get(r.status, 0) + 1
                if r.status >= 400:
                    errors.append(r.status)
        except Exception as exc:  # таймаут, сброс соединения
            errors.append(type(exc).__name__)
            codes['exc'] = codes.get('exc', 0) + 1
        await asyncio.sleep(0.05)  # «думает» 50 мс между действиями


async def main():
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout, connector=aiohttp.TCPConnector(limit=0)) as session:
        master_id, service_id = await discover(session)
        day = (datetime.date.today() + datetime.timedelta(days=2)).isoformat()
        urls = [
            f'{BASE}/api/companies/{SLUG}/info/',
            f'{BASE}/api/companies/{SLUG}/masters/',
            f'{BASE}/api/companies/{SLUG}/services/',
            f'{BASE}/api/companies/{SLUG}/slots/?master={master_id}&service={service_id}&date={day}',
            f'{BASE}/api/health/',
        ]
        print(f'{"клиентов":>9} {"запросов":>9} {"req/s":>7} {"p50 мс":>7} {"p95 мс":>7} {"p99 мс":>7} {"ошибок":>7}  коды')
        for level in LEVELS:
            latencies, errors, codes = [], [], {}
            stop = time.monotonic() + STAGE_SECONDS
            began = time.monotonic()
            await asyncio.gather(*[client(session, urls, stop, latencies, errors, codes) for _ in range(level)])
            took = time.monotonic() - began
            total = len(latencies) + codes.get('exc', 0)
            print(f'{level:>9} {total:>9} {total / took:>7.0f} {percentile(latencies, 50):>7.0f} '
                  f'{percentile(latencies, 95):>7.0f} {percentile(latencies, 99):>7.0f} '
                  f'{100 * len(errors) / max(total, 1):>6.1f}%  {json.dumps(codes)}', flush=True)
            await asyncio.sleep(5)  # пауза между уровнями


asyncio.run(main())
