# Исполняемые контракты

Команды запускаются из корня: `python -B -X utf8 scripts/<скрипт>.py`. Все входные JSON и артефакты с личными данными находятся в `.local/inputs`, `.local/drafts` и `.local/reports`. Скрипты проверяют структуру и согласия; истинность наблюдений и ограничения инструментов проверяет агент.

## Разрешения и отбор

`hr_policy.py authorize --operation application --platform hh` нормализует алиас и возвращает allowed/reason/grant_id. Отдельные операции: monitor, bot_reply, human_reply, profile_edit. Ответ false для последних двух требует проверки конкретного поручения владельца, а не подразумевает запрет уже данного поручения.

`hr_policy.py eligibility --json .local/inputs/vacancy.json` принимает нормализованную карточку: open, role_match, company_risk_checked, company_safe (bool); industry; employment; work_format; для офиса/гибрида location_match с проверенной связью с предпочтениями; test_before_interview; language_requirements (словарь язык → A1–C2, пустой словарь при явно отсутствующих требованиях); salary (published, verified, currency, period, basis, min/max); score. Неизвестное оставлять неизвестным. Высокий score не компенсирует needs_fact или ineligible. Отбор не принимает оффер и не меняет условия владельца.

## Пакет и ревью

`hr_workflow.py packet --json .local/inputs/packet.json`: platform, canonical_key, vacancy_url, vacancy, resume_ref, matched_fact_ids, cover_letter_text, campaign_id; при эксперименте experiment_id/variant; необязательные maximum_chars и length_unit. Для company_careers нужен official_route с evidence_url, verified_at, target_url, совпадающим с URL заявки.

Сначала команда выдаёт review_fingerprint и ready=false. Ревьюер получает текст, требования, разрешённые факты и fingerprint; результат review={fingerprint, outcome:"pass", reviewer, artifact_ref}. Это реальное смысловое ревью, скрипт его не выдумывает. При разрешённой делегации используй независимого агента; иначе покажи пакет владельцу для ревью. Повтори packet с review. Изменение текста, фактов, профиля, предпочтений, резюме, маршрута или лимита отменяет прежний pass. Ready не заменяет проверку формы, дневного остатка и claim.

Facts.json является списком. У передаваемых фактов id, confirmed=true и visibility=public. Resume_ref должен точно совпадать с одобренным resume_refs[platform]. Загрузка файла дополнительно требует automatic_uploads. Перед действием проверь действующее согласие и отсутствие новых вопросов вне фактов.

## Очередь и сбои

`hr_campaign.py add --help`, `claim --help`, `finish --help` описывают поля. Одному canonical_key вакансии соответствует одна отправка на всех площадках. Не используй только название должности: возьми устойчивый ID работодателя/реквизиции и ссылку, а совпадение между площадками проверь явно. Ключ чата включает точную заявку и идентификатор чата.

Один координатор добавляет задачи, выдаёт claims и сохраняет результаты. Исполнитель получает claim_token, сам не меняет общую очередь. Токен claim является локальным идентификатором владения задачей, не паролем. Он защищает от второго исполнителя и устаревшего finish. При истёкшем claim рискованная задача требует проверки статуса, а не автоматической повторной отправки. Перед выдачей новых claims координатор резервирует daily_maximum с учётом уже выполняемых заявок.

Для смены исполнителя текущей заявки: `hr_campaign.py handoff --task-id ID --worker-id OLD --new-worker-id NEW --claim-token TOKEN --artifact-ref .local/reports/reconciliation.json --reconciled`. Сначала останови прежнего исполнителя и сверь точную заявку. Только действующий running claim, другой свободный worker и локальное доказательство; lease не больше 900 секунд. Команда меняет token, сохраняет историю и не отправляет заявку. Для expired/uncertain сначала отдельная сверка, handoff их не обходит. Пачки до четырёх элементов работают в одном активном ходе по docs/economy.md, но каждый элемент получает свой claim после завершения предыдущего.

## Подтверждение отправки и CRM

`hr_workflow.py evidence --json .local/inputs/result.json` принимает canonical_key и evidence. В evidence: kind (application_status, bound_chat_status, official_receipt), vacancy_key, source_url, observed_at, status (submitted, delivered, already_applied), artifact_ref. Receipt должен подтверждать конкретную форму в локальном артефакте. Клик и заполненное поле не подходят.

Один исполнитель наблюдает устойчивый статус точной заявки. Координатор проверяет артефакт; повторно открывает сайт при сомнении, конфликте, неизвестном маршруте или выборочном контроле. После неясного результата сначала найди уже существующую заявку.

`hr_store.py observe-application --json .local/inputs/observed.json` сохраняет platform_observed. Входные поля CRM описаны в docs/data-model.md. Дополнительно передай canonical_key, campaign_id, discovery_source, submission_channel, profile_version, fact_version, matched_fact_ids, cover_hash, copy_review_ref, artifact_ref, evidence, packet_ref. Не приписывай историческому отклику вариант A/B без источника. Confirm-sent применяется к явному сообщению владельца об отправке.

## День, блокеры и повторные проверки

`hr_workflow.py day --day YYYY-MM-DD` показывает sent, цель, верхний предел, охват рынка/входящих, незавершённые задачи. Report_exists не закрывает день. Disabled означает, что кампания выключена. Все даты приводятся к часовому поясу владельца.

`record-day --json FILE`: day, artifact_ref; market_coverage/inbox_coverage как source → {state:complete/partial/error, evidence}; ready_to_send, pool_exhausted, shortfall_reason. При явной остановке владельцем campaign_state=stopped_by_user сохраняется независимо от последующих отчётов, не возобновляй этот день автоматически. Source_stats хранит измеренные unique_found, eligible, duplicates, sent, duration_seconds, agent_count, tokens и отдельные model/reasoning_effort по исполнителям. Неизвестное null. Предел отправок не доказывает исчерпание рынка; зафиксируй остаток. Незаконченная очередь не позволяет закрыть день. Охват рассчитывается по источникам на указанную дату через source_cadence; отсутствие назначенных источников отображается как not_required.

`record-company --json FILE`: company_id, risk_context, checked_at, evidence, safe. `company --json FILE` с company_id/risk_context проверяет свежесть. Новый контекст получателя или истёкший срок отменяет кэш.

`blocker --json FILE`: platform, reason, evidence, observed_at, notified. После фактического уведомления запиши notified=true. Повтор той же причины не уведомляется. Новый вход или наблюдаемое устранение фиксируй с reason=null.

После всей пачки новых отправок coordinator вызывает post-campaign, затем due-probes/ack-probe. Повтор пачки идемпотентен; сроки +5/+10/+60 минут от завершения. Между 10-й и 60-й минутой без активности нет пустых пятиминутных проверок. Подробности аргументов через --help. Schedule-mode возвращает режим и следующую проверку, но само не изменяет задание в приложении.

При ошибке приоритет имеет `hr_monitor_guard.py status` и docs/monitor-errors.md. Guard хранит попытки и cooldown отдельно от успешного inbox-checkpoint. Перед браузером реальное задание переводится на безопасный интервал, begin заранее записывает срок повтора; после любого исхода finish и обновление расписания обязательны. Просроченные probes не отменяют паузу. Нельзя продвинуть точку непрочитанного сообщения ради изменения времени следующей попытки.

`hr_store.py dashboard` создаёт `.local/DASHBOARD.md`; summary --since/--until/--as-of --json позволяет сравнивать фиксированные когорты. Недельный отчёт сохраняй как датированный снимок, не переписывай его новым состоянием без пометки.
