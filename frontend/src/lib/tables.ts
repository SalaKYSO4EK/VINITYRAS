const tableNames: Record<string, string> = {
  "publications.csv": "Публикации",
  "materials.csv": "Материалы",
  "devices.csv": "Образцы",
  "layers.csv": "Слои",
  "measurements.csv": "Измерения",
  "results.csv": "Результаты измерений",
  "Efficiency.csv": "КПД по публикациям",
  "issues.csv": "Замечания проверки",
};

const columnNames: Record<string, string> = {
  publication_id: "Публикация",
  metric_name: "Показатель",
  metric_value: "Значение",
  metric_unit: "Единица",
  result_id: "Код результата",
  raw_value: "Исходное значение",
  raw_unit: "Исходная единица",
  aggregation: "Агрегация",
  spread_value: "Разброс",
  spectrum: "Спектр освещения",
  light_intensity: "Интенсивность освещения",
  intensity_unit: "Единица интенсивности",
  temperature: "Температура измерения",
  efficiency_status: "Данные о КПД",
  device_label: "Образец",
  device_id: "Код образца",
  measurement_id: "Код измерения",
  source_file: "Файл",
  source_page: "Страница PDF",
  source_table: "Таблица в статье",
  source_row: "Строка таблицы",
  source_column: "Столбец таблицы",
  source_bbox: "Координаты ячейки в PDF",
  source_fragment: "Цитата",
  scan_direction: "Направление сканирования",
  title: "Название",
  note: "Примечание",
};

export const tableName = (name: string) => Object.hasOwn(tableNames, name) ? tableNames[name] : name;
export const columnName = (name: string) => Object.hasOwn(columnNames, name) ? columnNames[name] : name;
