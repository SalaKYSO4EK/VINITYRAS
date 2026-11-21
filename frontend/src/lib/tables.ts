const tableNames: Record<string, string> = {
  "publications.csv": "Публикации",
  "materials.csv": "Материалы",
  "devices.csv": "Образцы",
  "layers.csv": "Слои",
  "measurements.csv": "Измерения",
  "results.csv": "Результаты измерений",
  "issues.csv": "Замечания проверки",
};

const columnNames: Record<string, string> = {
  publication_id: "Публикация",
  metric_name: "Показатель",
  metric_value: "Значение",
  metric_unit: "Единица",
  device_label: "Образец",
  device_id: "Код образца",
  measurement_id: "Код измерения",
  source_file: "Файл",
  source_page: "Страница PDF",
  source_table: "Таблица в статье",
  source_fragment: "Цитата",
  scan_direction: "Направление сканирования",
  title: "Название",
  note: "Примечание",
};

export const tableName = (name: string) => Object.hasOwn(tableNames, name) ? tableNames[name] : name;
export const columnName = (name: string) => Object.hasOwn(columnNames, name) ? columnNames[name] : name;
