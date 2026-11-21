# Шаблон сдачи набора данных — предметная область 1 (тонкоплёночные солнечные элементы)

В этой папке пустые таблицы. В таком виде сдаётся итоговый набор данных, и в таком же виде оформлен эталон по dev. Тогда результаты всех команд можно проверить по эталону одним способом. Названия полей и допустимые значения взяты из разделов 3.2 и 3.3 условий кейса. Служебные поля, которых нет в условиях, отмечены ниже знаком (*).

## Файлы и связи

| Файл | Одна строка — это | Ключ | Ссылается на |
|---|---|---|---|
| `publications.csv` | статья корпуса | `publication_id` | — |
| `materials.csv` | материал поглотителя | `publication_id` + `material_id` | публикацию |
| `devices.csv` | прибор | `publication_id` + `device_id` | материал (`absorber_material_id`*) |
| `layers.csv` | слой прибора | `publication_id` + `layer_id` | прибор |
| `measurements.csv` | условия измерения | `publication_id` + `measurement_id` | прибор |
| `results.csv` | одно значение одного показателя | `publication_id` + `result_id` | прибор, измерение, материал |
| `issues.csv` | замечание проверки | `publication_id` + `issue_id`* | записи `results.csv` |

- `publication_id` — имя файла статьи без расширения: `D001`, `P017` и т. п.
- Остальные идентификаторы можно выбирать произвольно, но они должны быть уникальны внутри одной публикации: например, `M1`, `DV1`, `MS1`, `R001`.
- Таблицы связаны через эти идентификаторы. Например, `results.device_id` должен совпадать с `device_id` из `devices.csv` той же публикации.

## Общие соглашения

- Кодировка UTF-8, разделитель — запятая, десятичный знак — точка. Текст с запятыми заключается в кавычки по правилам CSV.
- **Пустая ячейка — значит, в статье этого нет.** Неизвестные значения не восстанавливаются по догадке (раздел 3.2 условий).
- Логические поля (`in_scope`, `mask_used`, `certified`) заполняются значениями `true` или `false`.
- Если в ячейке несколько значений, они перечисляются через `;`.
- В `materials`, `devices`, `layers`, `measurements` и `results` есть колонка `note`* — свободное примечание. Если формулировка в статье не укладывается в словарь (например, архитектура описана нестандартно), в поле словаря ставится ближайшее допустимое значение или пусто, а исходная формулировка уходит в `note`.
- `source_page` — номер страницы в PDF-файле, считая с 1. Это не номер страницы в журнале.
- `source_fragment` — короткая дословная цитата со страницы `source_page`, по которой можно найти значение. Если значение взято из таблицы, достаточно указать `source_table` и метку строки.

## Поля

### publications.csv
`publication_id; title; year; DOI; authors` — библиографические сведения. Авторы перечисляются через `;`.

`in_scope`* — относится ли статья к предметной области (раздел 3.1 условий): `true` или `false`. Строка заводится для **каждой** статьи корпуса, в том числе для исключённых.

`exclusion_reason`* — почему статья исключена. Заполняется, только если `in_scope=false`.

У исключённой статьи в остальных таблицах строк нет.

### materials.csv
| Поле | Значения |
|---|---|
| `material_class` | `perovskite`, `CIGS`, `CdTe`, `kesterite`, `Sb2Se3`, `thin-film Si`, `other` |
| `dimensionality` | `3D`, `2D`, `2D-3D` |
| `bandgap_method` | `Tauc`, `EQE`, `PL`, `cited` (значение взято из литературы) |
| `formula_normalized` | Для перовскита — вид A-B-X с раскрытыми MA, FA, Cs |
| `bandgap_unit` | `eV` |

### devices.csv
| Поле | Значения |
|---|---|
| `device_role` | `control`, `target`, `record` |
| `device_type` | `cell`, `minimodule`, `module` |
| `architecture` | `n-i-p`, `p-i-n`, `substrate`, `superstrate` |
| `substrate_type` | `rigid`, `flexible` |
| `area_definition` | `designated illumination`, `aperture`, `total` |
| `area_unit` | `cm2` |
| `absorber_material_id`* | Ссылка на поглотитель в `materials.csv` |

### layers.csv
`layer_order` — порядковый номер слоя, считая от подложки: 1, 2, …

`layer_function` — одно из значений: `front_contact`, `ETL`, `absorber`, `passivation`, `HTL`, `interlayer`, `recombination`, `back_contact`.

`thickness_unit` — `nm`: толщины в мкм переводятся в нм. Подложка (стекло, фольга) в `layers` не записывается — первым слоем идёт контакт на подложке. Слои, которых нет в списке функций, приводятся к ближайшему: буферный слой CdS и аналогичные n-слои — `ETL`, металлическая сетка — `front_contact`, просветляющее покрытие и прочие промежуточные слои — `interlayer`; исходное название — в `note`. Температура указывается в °C, время — в единицах из статьи с явной единицей, например `10 min`.

### measurements.csv
| Поле | Значения |
|---|---|
| `measurement_type` | `J-V`, `MPP tracking`, `EQE`, `stability` (темновая ВАХ — `J-V` с примечанием) |
| `scan_direction` | `reverse`, `forward`, `average` |
| `stability_protocol` | `ISOS-D`, `ISOS-L`, `ISOS-T`, `other` (если протокол ISOS в статье не назван, ставится `other`, условия описываются в `stress_conditions`) |

`spectrum` — например, `AM1.5G`. `light_intensity` и `intensity_unit` — например, `100` и `mW/cm2`.

Прямой и обратный ход развёртки оформляются как **разные** измерения.

### results.csv
Одна строка — один показатель для одного прибора при одних условиях измерения, включая направление развёртки и момент времени для стабильности. Значения для лучшего прибора и средние по партии — отдельные строки.

| Поле | Значения и правила |
|---|---|
| `metric_name` | `PCE`, `Voc`, `Jsc`, `FF`, `Rs`, `Rsh`, `HI`, `Jsc_EQE`, `stabilized_PCE`, `T80`, `retained_PCE` |
| `raw_value`, `raw_unit` | Значение и единица дословно как в статье, например `0.49` и `fraction` или `1123` и `mV` |
| `normalized_value`, `normalized_unit` | Значение в целевой единице: PCE, FF, HI, retained_PCE — `%`; Voc — `V`; Jsc, Jsc_EQE — `mA/cm2`; Rs, Rsh — `Ohm*cm2`; T80 — `h` |
| `metric_value`, `metric_unit` | Совпадают с `normalized_value` и `normalized_unit` |
| `aggregation` | `champion`, `mean`, `median`. **Пусто**, если в статье не сказано, лучший это прибор или среднее |
| `spread_value`, `n_devices` | Разброс (± или стандартное отклонение) и число приборов, если они указаны |
| `time_point`, `time_unit` | Момент измерения для результатов стабильности |
| `result_origin` | `primary` — собственный результат авторов; `cited` — значение из другой работы |
| `cited_publication_id` | Для `cited`: ссылка в статье, например `[12]` или `[19–21]`. `device_id` и `measurement_id` у таких строк остаются пустыми |
| `extraction_status` | `extracted`; `present_not_extractable` (значение есть, но извлечь его нельзя: например, оно только в supplementary или только на графике); `absent` |
| `duplicate_of`* | Если то же значение того же прибора упомянуто ещё раз (в аннотации, в тексте, в другой таблице), повтор записывается отдельной строкой со своим `source_*`. В `duplicate_of` указывается `result_id` основной записи. У основной записи поле пустое |

Показатели, которых нет в списке `metric_name` (дефицит Voc, Jmpp, Vmpp, AVT и др.), можно записывать с понятным названием. При оценке извлечения они не учитываются.

### issues.csv
| Поле | Содержание |
|---|---|
| `record_ids` | `result_id` записей, к которым относится замечание, через `;` |
| `check_name` | Какое правило сработало: например, `jv_consistency`, `voc_limit`, `range`, `eqe_consistency`, `champion_vs_mean`, `hysteresis_index` (правила из раздела 3.3 условий), а также `contradiction` — одно значение в разных местах статьи расходится |
| `description` | Что именно не согласуется: числа с обеих сторон |
| `source_page`, `source_fragment` | Где это видно в статье |

## Пример строк

```
results.csv
D021,R001,DV-B4,MS1,M-RFMS,PCE,2.45,%,mean,0.39,20,,,2.45,%,2.45,%,primary,,13,3.4,Table 4,,"Table 4, B4",extracted,,
D021,R002,DV-B4,MS1,M-RFMS,PCE,3.85,%,champion,,,,,3.85,%,3.85,%,primary,,13,3.4,Table 4,,"Table 4, B4 (record)",extracted,,
D021,R003,DV-B4,MS1,M-RFMS,PCE,5.25,%,champion,,,,,5.25,%,5.25,%,primary,,13,3.4,,Figure 10,exhibited PCE = 5.25%,extracted,,
D021,R004,DV-B4,MS1,M-RFMS,PCE,5.25,%,champion,,,,,5.25,%,5.25,%,primary,,1,Abstract,,,achieved a maximum efficiency of 5.25%,extracted,R003,

issues.csv
D021,I1,R002;R003,contradiction,"Лучший прибор B4: в Table 4 PCE 3.85 %, в тексте и на рис. 10 — 5.25 %",13,exhibited PCE = 5.25%
```
