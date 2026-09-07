# Синтетические fixtures

Канонический набор находится в `backend/app/sources/mock/listings.json`: он включён в пакет приложения, чтобы MockSourceAdapter работал и в Docker. Тесты используют этот же файл без сети. Все записи вымышлены, URL ведут на зарезервированный `example.invalid`; реальных фотографий, телефонов и данных продавцов нет.

Дата создания: 2026-09-06. Версии parser/normalizer: 1. Сценарии адаптера: normal, empty, price_drop, removed, unavailable, rate_limited, auth_required, parser_error. Время наблюдения передаётся в normalize явно, тесты используют фиксированное UTC-время.

Captured fixtures Avito/Auto.ru/Drom добавляются в Phase 4 по одному источнику. Наличие разрешений подтверждено пользователем; для подключения ещё нужны технические параметры доступа.
