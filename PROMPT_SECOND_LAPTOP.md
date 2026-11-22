Ты — Codex на Sol на втором ноутбуке. У тебя полный доступ к системе и осталось 10% недельного лимита. Сам настрой локальный API Qwen для NEXUS. На первом ноутбуке остаются веб-приложение, PDF/ZIP, NuExtract, проверка источников и CSV. Этот ноутбук получает только текст критериев и возвращает JSON. Ограничь работу настройкой сервера и одной короткой генерацией. Сразу заведи QWEN_STATE.md в рабочей папке и обновляй после выбора модели и запуска. При прерывании оставь фактическое состояние, лог и точную следующую команду.

1. Определи систему, доступную память, видеокарту, LAN-адрес и существующий сервер. На Windows выполни в PowerShell:
    Get-CimInstance Win32_OperatingSystem | Select Caption, OSArchitecture, FreePhysicalMemory
    Get-CimInstance Win32_VideoController | Select Name, DriverVersion
    Get-Command llama-server, nvidia-smi -ErrorAction SilentlyContinue
    Get-NetIPConfiguration | Where-Object IPv4Address | Select InterfaceAlias, IPv4Address, IPv4DefaultGateway
    Get-NetTCPConnection -State Listen -LocalPort 8080 -ErrorAction SilentlyContinue
Если есть nvidia-smi, выполни nvidia-smi --query-gpu=name,memory.total,memory.free --format=csv. Проверь рабочую папку и известную папку llama.cpp. На Linux эквиваленты — free -h, command -v llama-server, ip -br addr, ss -ltn. Используй уже установленный совместимый сервер, если он умеет chat completions и JSON Schema.

2. Для второго ноутбука сначала оцени Qwen3-4B Q4_K_M. В проекте использовалась Qwen2.5-1.5B-Instruct Q4_K_M, обе модели уже проверены на первом ноутбуке. Для Qwen3 перенеси по локальной сети или с USB файл tmp/nexus-criteria-qa/qwen-models/Qwen3-4B-Q4_K_M.gguf в .\models\ рабочей папки сервера на втором. Размер 2 497 280 256 байт, SHA-256 7485fe6f11af29433bc51cab58009521f205840f5b4ae3a32fa7f92e8534fdf5. Сверь Get-FileHash -Algorithm SHA256 -LiteralPath $model. Для меньшей модели исходник model_bundle/models/criteria/qwen2.5-1.5b-instruct-q4_k_m.gguf (1 117 320 736 байт, SHA-256 6a1a2eb6d15622bf3c96857206351ba97e1af16c30d7a74ee38970e434e9407e). Из проекта нужен один выбранный GGUF. На первом штатно используется llama-completion.exe b8157. На втором нужен llama-server со своими библиотеками. Перед скачиванием, установкой ПО или переходом на другую модель сообщи причину, объём, требования к памяти и дождись моего разрешения.

3. Оба ноутбука подключены к одной точке доступа телефона. Выбери IPv4 интерфейса этой сети, свободный TCP-порт (предпочтительно 8080) и один слот. Штатные настройки первого ноутбука — контекст 8192, 4 CPU-потока, температура 0, до 1800 токенов ответа. Попробуй сохранить контекст. На Windows задай $exe и $model как полные пути к серверу и GGUF, $lan как найденный IPv4, $iface как имя интерфейса, $port как порт. Проверь флаги через & $exe --help. Команда запуска в отдельном PowerShell:
    & $exe -m $model --alias nexus-qwen3-4b --host $lan --port $port -c 8192 -np 1 -t 4 -ngl 0
Сохрани start-qwen.ps1 с фактическими путями и параметрами. Для GPU выбери доступный в установленной сборке бэкенд и количество слоёв с учётом свободной видеопамяти. При 4 ГБ учитывай кэш контекста и резерв драйверу. При нехватке видеопамяти сохрани контекст через частичную загрузку слоёв в GPU или CPU. Уменьшение контекста отрази в отчёте. При занятом порте выбери свободный. Для согласованной меньшей модели задай alias nexus-qwen2.5-1.5b.

4. Если входящий TCP заблокирован, из повышенного PowerShell создай одно правило для выбранной программы и интерфейса. В $peer укажи IP первого ноутбука, если известен, иначе LocalSubnet:
    New-NetFirewallRule -DisplayName "NEXUS Qwen LAN" -Direction Inbound -Action Allow -Protocol TCP -Program $exe -LocalAddress $lan -LocalPort $port -RemoteAddress $peer -InterfaceAlias $iface
На Linux используй действующий брандмауэр с теми же ограничениями. Оставь доступ в пределах этой сети. Проверь готовность и фактический ID:
    $base = "http://${lan}:$port/v1"
    $ids = (Invoke-RestMethod "$base/models" -TimeoutSec 10).data.id
    $id = 'nexus-qwen3-4b'   # Для другого сервера подставь фактический ID выбранной модели.
    if ($id -notin $ids) { throw "ID модели отсутствует в /v1/models: $ids" }
Выполни ровно одну короткую генерацию, проверяющую и JSON Schema:
    $schema = @{type='object';properties=@{metric=@{type='string';enum=@('pce')}};required=@('metric');additionalProperties=$false}
    $request = @{model=$id;messages=@(@{role='system';content='Return JSON.'},@{role='user';content='Return JSON with metric pce.'});temperature=0;max_tokens=48;stream=$false;response_format=@{type='json_schema';json_schema=@{name='probe';strict=$true;schema=$schema}}}
    if ($id -match 'qwen3') { $request.chat_template_kwargs = @{enable_thinking=$false}; $request.messages[1].content += "`n/no_think" }
    $body = $request | ConvertTo-Json -Depth 12
    $reply = Invoke-RestMethod "$base/chat/completions" -Method Post -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($body)) -TimeoutSec 120
    ($reply.choices[0].message.content | ConvertFrom-Json).metric
Ожидается pce. Контракт приложения — GET /v1/models и POST /v1/chat/completions, роли system/user, ответ в choices[0].message.content в виде строки JSON. Для разметки правил клиент передаёт JSON Schema с идентификаторами фрагментов. Если тест завершился ошибкой, сохрани её текст.

5. Для первого ноутбука верни одну команду с подставленным адресом: (Invoke-RestMethod 'http://IP:ПОРТ/v1/models' -TimeoutSec 10).data.id. Если проверка на втором проходит, а с первого нет, проверь адрес, интерфейс, правило и изоляцию клиентов точки доступа. Запасной вариант при наличии кабеля и портов — прямой Ethernet. Сначала проверь полученные адреса. Если нужно, выбери свободную подсеть, например 192.168.77.0/24. Только на свободных Ethernet-интерфейсах назначь второму .2 и первому .1 без шлюза; пример для второго: New-NetIPAddress -InterfaceAlias 'Ethernet' -IPAddress 192.168.77.2 -PrefixLength 24. Передай соответствующую команду для первого. Перепривяжи сервер и правило к Ethernet, повтори GET /v1/models с первого. Учитывай смену IP при переподключении.

6. Оставь сервер запущенным. Верни base_url с /v1, model в точности из /models, фактический контекст и режим CPU/GPU, команду повторного запуска, результат генерации и отдельно статус проверки с первого ноутбука. Для первого приложения выдай готовый JSON с base_url, model и timeout_seconds (180) для model_bundle/config/qwen-api.json. Если первый ноутбук тебе недоступен, укажи, что проверка между устройствами ожидается.
