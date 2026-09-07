# Нормализованные модели

Статус: контракт Phase 0. DTO реализуются на Pydantic в Phase 1; это описание типов, не уже существующий код.

## Общие соглашения

- UUID для внутренних ID, UTC timezone-aware datetime для времени; UI переводит даты в пользовательский часовой пояс.
- `null` = неизвестно/не предоставлено. Нельзя заменять неизвестный пробег, владельцев или цену нулём.
- Марки, модели, поколения и регионы имеют внутренние canonical keys. Slug/ID конкретного сайта хранится только в адаптере. Таблицы aliases версионируются; нераспознанное значение не угадывается.
- Цена: Decimal в денежных единицах + код валюты; JSON передаёт десятичное значение строкой, БД — NUMERIC(14,2). На MVP поиск по RUB; другая валюта не конвертируется молча и не участвует в рублёвом диапазоне.
- Пробег — целые км, мощность — л.с., объём — Decimal литров с точностью 0.001. Преобразования миль/см³/кВт выполняет normalizer с тестами и указанием происхождения.
- Все строковые значения ограничены по длине; source_metadata — до 16 KiB, очищенный текст description — до 30 000 символов, images — максимум 6 выбранных для обработки URL. Некорректный обязательный ID/URL отклоняет запись с диагностикой, не весь run.

## UnifiedSearchFilters

`schema_version=1`. Фильтры необязательны, кроме заданных зависимостей. Неизвестные ключи запрещены (`extra=forbid`). Пустые строки нормализуются в null, массивы — без дублей. Для всех диапазонов from ≤ to.

| Поле | Тип и правило |
| --- | --- |
| make | canonical key или null |
| model | canonical key или null; требует make |
| generation | canonical key или null; требует model |
| year_from, year_to | int или null; 1886…текущий год + 1 |
| price_from, price_to | Decimal ≥ 0 или null; RUB |
| mileage_from, mileage_to | int ≥ 0 или null, км |
| region | canonical region/city key или null; при выборе города хранит его тип |
| radius_km | int 0…1000 или null; требует однозначного центра region |
| body_types | список BodyType; пустой список означает любой кузов |
| transmission | Transmission или null |
| engine_type | EngineType или null |
| engine_volume_from, engine_volume_to | Decimal > 0 или null, л |
| power_from, power_to | int > 0 или null, л.с. |
| drive_type | DriveType или null |
| steering_wheel | left/right или null |
| owners_max | int ≥ 1 или null |

Начальные enum: BodyType=`sedan,hatchback,liftback,wagon,suv,coupe,convertible,pickup,minivan,van,other`; Transmission=`manual,automatic,robot,cvt`; EngineType=`petrol,diesel,hybrid,electric,gas,other`; DriveType=`front,rear,all`. Неизвестный тип в listing — null, не `other`; `other` означает известный тип вне перечисления. Особенности AWD/4WD, гибрида и коробки не додумываются по названию модели.

Объём ДВС у электромобиля отсутствует. Радиус считается от согласованного центра по геодезическому расстоянию; точность до города отмечается как approximate. Если источник поддерживает только регион без радиуса и нет координат, radius нельзя объявить выполненным.

### Выполнение фильтров

Для каждого активного фильтра адаптер возвращает `native`, `local`, `unsupported` или `unverified` и пояснение. Локальная проверка даёт `PASS|FAIL|UNKNOWN`.

- В основную выдачу попадают только объявления без FAIL и UNKNOWN по обязательным заданным фильтрам.
- UNKNOWN выводятся отдельно по явному переключателю «Показать непроверенные», с причиной (например, число владельцев не указано). Отсутствие поля не становится подтверждённым совпадением.
- Неподдерживаемый параметр не отправляется в источник; при доступном поле применяется локально. Если проверить его нельзя, источник получает filter warning и найденные карточки остаются непроверенными.
- Если все записи нельзя проверить, UI показывает «Нет подтверждённых совпадений; есть непроверенные», а не «Машин нет».
- Поддержка собственного кабинета/API дилера не означает поддержку публичного поиска тех же фильтров.

## NormalizedListing

Обязательны: source, source_listing_id, source_url, title, observed_at, field_presence. Остальные фактические поля допускают null, если ниже не указан default.

| Поля | Тип / семантика |
| --- | --- |
| source | `avito`, `auto_ru`, `drom`, `mock` |
| source_listing_id | str до 128; уникален только внутри source |
| source_url | абсолютный HTTPS URL карточки, проверенный adapter allowlist |
| title | очищенная строка до 500 |
| make, model, generation | canonical keys |
| year | int, валидный год |
| body_type, engine_type, transmission, drive_type | enum выше |
| engine_volume | Decimal, литры |
| power_hp | int > 0 |
| mileage_km | int ≥ 0 |
| color | canonical color key, неизвестный оттенок не угадывается |
| steering_wheel | left/right |
| price, currency | Decimal ≥ 0, ISO currency; неизвестная цена null |
| region, city | canonical keys; до улицы/дома не сохраняем |
| latitude, longitude, location_precision | опциональный центр города; `city` или `region`, без точного адреса продавца |
| owners_count | int ≥ 0, только явно указанное значение |
| seller_type | `private`, `dealer`, `unknown`; default unknown |
| seller_name | nullable, отключено политикой MVP по умолчанию |
| description | plain text, контакты удалены |
| published_at | время публикации, только если источник его явно сообщил |
| observed_at | время успешного наблюдения, задано worker |
| first_seen_at, last_seen_at | в persisted/read модели; вычисляет ingestion, адаптер не переопределяет |
| status | `ACTIVE`, `REMOVED`, `UNKNOWN`; default UNKNOWN. `RELISTED` — персональный read status из relist_links |
| status_evidence | тип подтверждения, время и безопасная причина |
| main_image_url | проверенный HTTPS URL, nullable |
| images | список ImageRef, default []; только выбранное подмножество |
| features | типизированные опциональные признаки: trim, interior_color, panoramic_roof и т.п. |
| source_metadata | очищенный source-specific object, default {} |
| parser_version, normalization_version | строки версий для диагностики |
| field_presence | набор фактически прочитанных полей |
| warnings | список field + code + safe_message |

`field_presence` нужен для частичного search/detail ответа: пропущенное описание не затирает ранее полученное. Явно подтверждённое очищенное поле можно обнулить, только если оно присутствует в field_presence. Ошибка декодирования поля добавляет warning и не эквивалентна его удалению. Приоритет и свежесть полей фиксируются в ingestion: более старый ответ не откатывает новые значения.

`ImageRef`: original_url, position, role=`main|dedup`, source_image_id (если дан). Сам fingerprint — отдельная сущность с algorithm/version; raw bytes в DTO не передаются. Phone/email/full VIN в модель MVP не входят. Возможный будущий VIN-сигнал потребует отдельного согласованного минимального представления.

## Read DTO и события

`VehicleCluster`: id, user_id (внутренний), version, first_seen_at, last_seen_at, representative_listing_id, listings, active_price_min/max, currency, field_conflicts, duplicate_explanation, user_state. Представитель выбирается из свежих активных доступных объявлений с наибольшей полнотой; null не заменяет известный факт. Персональные поля не входят в общий Listing.

`SavedSearch`: id, user_id, name, filters, filters_version, enabled, enabled_sources, refresh_interval_seconds (default 1500), created_at, last_checked_at, last_completed_at, next_due_at, is_temporary, expires_at. `last_checked_at` означает завершение попытки, успех определяется отдельно по каждому источнику.

`SavedSearchSourceState`: search_id, source, status, last_attempt_at, last_success_at, last_error, last_result_count, last_run_id, retry_after. Число результатов не обнуляется при ошибке как будто получена пустая выдача; сохраняется результат последнего успеха с его временем.

`DuplicateScore`: total_score ∈ [0,1], confidence=`high|medium|low|insufficient_evidence`, individual_signals (значение/доступность/пояснение каждого), evidence_coverage, strong_signal, vetoes, decision=`auto_merge|possible_duplicate|different|insufficient_evidence`, explanation, algorithm_version, config_version. Score не является вероятностью.

`Event`: id, user_id, search_id (nullable), listing_id, cluster_id_at_event, type, occurred_at, payload_version, минимальный payload old/new/snapshot reference, read_at. Типы: NEW_LISTING, PRICE_DROP, PRICE_INCREASE, LISTING_REMOVED, LISTING_RETURNED, PROBABLE_RELIST, POSSIBLE_DUPLICATE. В payload нет стороннего HTML и секретов.
