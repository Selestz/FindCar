# Развёртывание на VPS

Конфигурация предназначена для Ubuntu с Docker Compose и системным Nginx. База и API доступны только внутри сети Compose; frontend слушает `127.0.0.1:8080`. Внешний HTTPS обслуживает Nginx.

## Конфигурация

Создайте `.env` из `.env.example`, задайте случайный `POSTGRES_PASSWORD`, `APP_ORIGIN=https://testerio.ru`, `COOKIE_SECURE=true`. Для реальных источников включите `DROM_ENABLED=true` и `AUTO_RU_ENABLED=true`. Avito остаётся отключённым. Пароли и SSH-ключи не включаются в Git.

Из `/opt/findcar`:

```sh
sudo docker compose -f docker-compose.yml -f deploy/compose.production.yml config --quiet
sudo docker compose -f docker-compose.yml -f deploy/compose.production.yml up --build -d
sudo docker compose exec backend python -m app.auth.cli admin
curl --fail http://127.0.0.1:8080/api/ready
```

Admin CLI нужен только при первом запуске пустой БД. Используйте новый пароль длиной не менее 12 символов. Файл `compose.production.yml` добавляет автоматический перезапуск постоянных сервисов и ограничение размера журналов. Одноразовый migrate выполняется до API и worker.

`nginx.conf` использует существующий сертификат Certbot для testerio.ru. В другом окружении сначала получите сертификат и измените домен/пути. Конфигурация перенаправляет HTTP и www на единственный origin `https://testerio.ru`, чтобы cookie и Origin/CSRF работали согласованно.

Перед включением сохраните предыдущий virtual host. Не включайте одновременно два server block с одинаковыми server_name/listen. После замены выполните `sudo nginx -t`, затем `sudo systemctl reload nginx`. При ошибке проверки верните предыдущий virtual host. Не удаляйте базу предыдущего проекта.

## Обновление и диагностика

```sh
cd /opt/findcar
git pull --ff-only
sudo docker compose -f docker-compose.yml -f deploy/compose.production.yml up --build -d
sudo docker compose ps
sudo docker compose logs --tail=50 backend worker
curl --fail https://testerio.ru/api/ready
```

В командах запуска используйте оба Compose-файла, чтобы сохранить политику перезапуска и журналов. `docker compose down` не нужен для обычного обновления. Не используйте `down -v`: это удаляет данные.

## Резервная копия на сервере

Утилиты PostgreSQL находятся в контейнере postgres:

```sh
cd /opt/findcar
umask 077
mkdir -p .local/backups
backup_path=".local/backups/findcar-$(date -u +%Y%m%dT%H%M%SZ).dump"
(set -C; sudo docker compose exec -T postgres pg_dump -U findcar -Fc --no-owner --no-acl findcar > "$backup_path")
```

Архив содержит приватные данные; сохраняйте копию также вне VPS. Эта команда создаёт обычный PostgreSQL dump без manifest для `app.backup verify`. Проверка manifest и восстановления через Python CLI описана в основном README и требует доступных pg_dump/pg_restore на машине запуска.

## Тесты контейнеров

Тестам нужна отдельная БД с суффиксом `_test`; они очищают её. Не передавайте рабочую БД. Из контейнера backend можно сформировать тестовый URL без вывода пароля:

```sh
sudo docker compose exec -T postgres createdb -U findcar findcar_test
sudo docker compose exec -T backend python - <<'PY'
import os, subprocess
from sqlalchemy.engine import make_url
env = os.environ.copy()
env['TEST_DATABASE_URL'] = make_url(env['DATABASE_URL']).set(database='findcar_test').render_as_string(hide_password=False)
raise SystemExit(subprocess.run(['pytest', 'tests', '-q', '-p', 'no:cacheprovider'], env=env).returncode)
PY
```

Удаляйте `findcar_test` после тестов только если создали её специально для этого прогона. HTTP TestClient явно отключает Secure cookie в своей фикстуре; работающий сайт сохраняет `COOKIE_SECURE=true`, а отдельный тест проверяет Secure-заголовок.

## Возврат HappyMarket на этой машине

При первом переключении сохранены `/etc/nginx/sites-available/happymarket` и копия `/etc/nginx/findcar-rollback/happymarket.conf`. Контейнеры и БД HappyMarket остаются на сервере. Для возврата сначала убедитесь, что `http://127.0.0.1:3000/api/health` отвечает, затем отключите только ссылку `/etc/nginx/sites-enabled/findcar`, восстановите ссылку на прежний virtual host, проверьте `nginx -t` и перезагрузите Nginx. FindCar и его БД удалять не требуется.
