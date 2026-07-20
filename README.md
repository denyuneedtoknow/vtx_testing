# VTX Power Logger

Консольний застосунок для Windows, який раз на секунду зчитує середню потужність з датчика R&S NRP-Z11 і, за потреби, записує її у CSV.

## Передумови

1. Підключіть NRP-Z11 до ноутбука через сумісний USB-адаптер/кабель.
2. Встановіть **R&S NRP Toolkit** (VXI PnP driver). Драйвер надає `rsnrpz_64.dll`.
3. Встановіть Python 3.11+ для Windows. Для першого етапу інші Python-пакети не потрібні.

## Запуск

У PowerShell у каталозі проєкту:

```powershell
py .\vtx_power_logger.py --frequency-hz 2700000000
```

За замовчуванням застосунок відкриває перший доступний датчик (`--resource *`), застосовує частотну корекцію 2.7 ГГц, усереднює 16 вибірок і друкує один результат на секунду. Зупинка — `Ctrl+C`.

Запис у CSV:

```powershell
py .\vtx_power_logger.py --frequency-hz 2700000000 --csv .\logs\test-001.csv
```

CSV містить часову позначку UTC, час від початку тесту та потужність у mW. Файл дописується, тому попередні вимірювання не стираються.

## Якщо драйвер не знайдено

Передайте шлях до бібліотеки явно:

```powershell
py .\vtx_power_logger.py --dll "C:\Program Files\Rohde-Schwarz\NRP Toolkit\bin\rsnrpz_64.dll"
```

Або задайте змінну середовища `RSNRPZ_DLL` із цим шляхом.

## Пакування для закритого тестового ноутбука

На **Windows-машині розробки**, де є вихідний код і Python, виконайте:

```powershell
.\build.ps1
```

Скрипт створить `dist\VTX-Testing.zip`. Переносьте на тестовий ноутбук лише цей
архів, розпакуйте його та запускайте:

```powershell
.\VTX-Testing\VTX-MSP-Probe\VTX-MSP-Probe.exe --port COM83
.\VTX-Testing\VTX-Power-Logger\VTX-Power-Logger.exe --frequency-hz 2700000000 --csv .\logs\test.csv
```

Python, `pip`, GitHub та вихідний код на тестовому ноутбуці не потрібні. Проте
**R&S NRP Toolkit** має бути встановлений там для USB-драйвера NRP-Z11. За
потреби DLL можна додати до збірки так:

```powershell
.\build.ps1 -DriverDll "C:\Program Files\Rohde-Schwarz\NRP Toolkit\bin\rsnrpz_64.dll"
```

Це пакує DLL біля logger, але не замінює встановлення USB-драйвера.

### Передавання коду з macOS

На Mac у корені проєкту виконайте:

```zsh
./package_for_windows.sh
```

Скрипт створить `dist/VTX-Testing-source.zip` лише з потрібними для Windows
збірки файлами: двома Python-інструментами, `build.ps1`, `requirements.txt`,
`configs/` і README. Перенесіть цей архів на Windows, розпакуйте та виконайте
`build.ps1` за інструкцією вище.

## Обмеження першого етапу

Застосунок не керує VTX і не має GUI. Він лише вимірює та стабільно логує RF-потужність — це базова перевірка вимірювального тракту перед наступними етапами.

## Калібрування тракту

Профіль [configs/measurement_path_control.json](configs/measurement_path_control.json) містить
Level Offset у dB для вимірювального тракту. Це додатна поправка, яку потрібно
застосовувати до показу NRP-Z11. Для FT VTX 2.7 (2700–3000 MHz) вона становить
`+0.70 dB` на всіх каналах.

## Перевірка VTX через Flight Controller

`vtx_msp_probe.py` лише читає поточну VTX-конфігурацію через MSP; він не змінює
налаштувань Flight Controller або VTX. Закрийте Betaflight Configurator, бо він
використовує той самий COM-порт Flight Controller.

На Windows встановіть залежність і запустіть probe, вказавши COM-порт FC:

```powershell
python -m pip install -r requirements.txt
python .\vtx_msp_probe.py --port COM5
```

Вивід містить поточні `band`, `channel`, `frequency_mhz`, `power_index` і стан
готовності VTX. Цей інструмент є безпечним першим етапом перед реалізацією
автоматичної зміни параметрів через IRC Tramp.

## Одноразовий тест A1 + вимірювання

`vtx_a1_measure.py` — інтерактивний одноточковий тест. Він показує поточний
стан VTX, пропонує обрати модель, перемикає на її A1, чекає 3 секунди,
перечитує стан через FC та лише після успішного підтвердження вимірює потужність
NRP-Z11. Поточні power index та pit mode скрипт не змінює.

Закрийте Betaflight Configurator і переконайтеся, що VTX підключений до NRP-Z11
через атенюатор. Запуск:

```powershell
python .\vtx_a1_measure.py --port COM83
```

Якщо не передавати `--profile`, скрипт запропонує вибрати один із локальних
профілів VTX. Це зручно для FT VTX 5.8 і 7.2:

```powershell
python .\vtx_a1_measure.py --port COM83
```

Для іншого VTX передайте відповідний профіль, наприклад:

```powershell
python .\vtx_a1_measure.py --port COM83 --profile .\configs\vtx_profiles\ft_vtx_2_7_4w.json
```

## Три-точковий тест FT VTX 3.3 на 4 W

`vtx_three_point_test.py` вимірює FT VTX 3.3 на B1 / 3170 MHz, A1 / 3330 MHz
та A8 / 3470 MHz у режимі 4 W. Після тесту він повертає VTX до 25 mW, зберігаючи
останню частоту. Повернення до 25 mW також виконується після помилки вимірювання,
якщо FC все ще доступний.

На кожній частоті скрипт після перемикання спостерігає потужність 5 секунд із
кроком 0.5 секунди. У терміналі він показує округлені середнє, мінімум, максимум
і зміну між першим та останнім виміром. Усі неокруглені вибірки автоматично
зберігаються у `logs/vtx-test-<UTC timestamp>.csv` — це основа для майбутнього
графіка та виявлення падіння потужності під час прогріву.

```powershell
python .\vtx_three_point_test.py --port COM83
```

Скрипт запропонує вибрати 3.3 / 5.8 / 7.2. Для кожної моделі використовується
мінімальна доступна частота, частота найближча до математичної середини діапазону
та максимальна доступна частота — а не фіксовані назви бендів. Поточні точки:

- FT 3.3: 3170, 3330, 3470 MHz;
- FT 5.8: 4867, 5402, 5945 MHz;
- FT 7.2: 6110, 6710, 7210 MHz.

Для FT 5.8 бенди J/U/O/H лежать за межами старого восьмибендового MSP-кодування,
тому скрипт задає їх за прямою частотою в MHz. У відповідь FC може показати
`band: 0`, що для такого режиму є очікуваним.

Для серії однакових VTX вкажіть модель прямо в команді — меню не з'явиться:

```powershell
python .\vtx_three_point_test.py --port COM83 --vtx 7.2
python .\vtx_three_point_test.py --port COM83 --vtx 5.8
python .\vtx_three_point_test.py --port COM83 --vtx 3.3
```

Точки, power index та еталонні потужності зберігаються у
`configs/test_plans/ft_vtx_3_3_three_point_4w.json`. Еталони поки що тільки
виводяться в термінал; пороги PASS/FAIL додамо окремо.
