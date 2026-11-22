"""Read browser uploads and stage PDF files without extracting archive paths."""

import io
import lzma
import re
import shutil
import stat
import zipfile
import zlib
from email import policy
from email.parser import BytesFeedParser
from pathlib import Path


MIB = 1024 * 1024
MAX_UPLOAD = 128 * MIB
MAX_PDF_BYTES = 64 * MIB
MAX_TOTAL_PDF_BYTES = 256 * MIB
MAX_PDFS = 100
MAX_CRITERIA_CHARS = 32_000
_RESERVED_NAME = re.compile(r"(?:CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³]|CONIN\$|CONOUT\$) *(?:\.|$)", re.I)
_PAGE_SELECTION = re.compile(r"[1-9][0-9]*(?:-[1-9][0-9]*)?(?:,[1-9][0-9]*(?:-[1-9][0-9]*)?)*")


class InvalidUpload(ValueError):
    def __init__(self, message, *, status=400, file_issues=()):
        super().__init__(message)
        self.status = status
        self.file_issues = file_issues


def valid_filename(name):
    return (
        0 < len(name) <= 160
        and name == name.strip()
        and not name.endswith(".")
        and not _RESERVED_NAME.match(name)
        and not any(ord(char) < 32 or char in '<>:"/\\|?*' for char in name)
    )


def read_form(headers, read):
    content_type = headers.get("Content-Type", "")
    if headers.get_content_type() != "multipart/form-data" or not headers.get_boundary():
        raise InvalidUpload("Отправьте форму загрузки как multipart/form-data.", status=415)
    if not content_type.isascii() or "\r" in content_type or "\n" in content_type:
        raise InvalidUpload("Некорректный заголовок Content-Type.")
    if headers.get("Transfer-Encoding") or headers.get("Content-Length") is None:
        raise InvalidUpload("Требуется Content-Length.", status=411)
    try:
        length = int(headers["Content-Length"])
    except ValueError as exc:
        raise InvalidUpload("Некорректный размер запроса.") from exc
    if length <= 0:
        raise InvalidUpload("Загрузите хотя бы один PDF или ZIP.")
    if length > MAX_UPLOAD:
        raise InvalidUpload("Размер загрузки превышает 128 МиБ.", status=413)

    prefix = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("ascii")
    parser = BytesFeedParser(policy=policy.default)
    parser.feed(prefix)
    remaining = length
    while remaining:
        data = read(min(remaining, MIB))
        if not data:
            raise InvalidUpload("Загрузка оборвалась.")
        parser.feed(data)
        remaining -= len(data)
    form = parser.close()
    if not form.is_multipart() or form.defects:
        raise InvalidUpload("Не удалось прочитать форму загрузки.")

    criteria = None
    criteria_pdf = None
    criteria_pages = None
    uploads = []
    for part in form.iter_parts():
        name = part.get_param("name", header="Content-Disposition")
        filename = part.get_filename()
        if part.is_multipart():
            raise InvalidUpload("Вложенная форма загрузки не поддерживается.")
        payload = part.get_payload(decode=True)
        if name == "criteria" and filename is None and criteria is None:
            try:
                criteria = payload.decode("utf-8-sig").strip()
            except UnicodeDecodeError as exc:
                raise InvalidUpload("Критерии должны быть в UTF-8.") from exc
        elif name == "criteria_pdf" and filename and valid_filename(filename) and criteria_pdf is None:
            if Path(filename).suffix.lower() != ".pdf" or b"%PDF-" not in payload[:1024]:
                raise InvalidUpload("В поле критериев должен быть файл PDF.", status=415)
            if len(payload) > MAX_PDF_BYTES:
                raise InvalidUpload("PDF с критериями превышает 64 МиБ.", status=413)
            criteria_pdf = (filename, payload)
        elif name == "criteria_pages" and filename is None and criteria_pages is None:
            try:
                value = payload.decode("utf-8-sig").translate(str.maketrans("–—", "--"))
            except UnicodeDecodeError as exc:
                raise InvalidUpload("Неправильный диапазон страниц PDF.") from exc
            criteria_pages = re.sub(r"\s*([,-])\s*", r"\1", value.strip())
            if criteria_pages and (len(criteria_pages) > 300 or not _PAGE_SELECTION.fullmatch(criteria_pages)):
                raise InvalidUpload("Укажите страницы в формате 3-6 или 3,5,7-9.")
        elif name == "files" and filename is not None:
            uploads.append((filename, payload))
        else:
            raise InvalidUpload("Передайте статьи в полях files. Для своих критериев используйте criteria или criteria_pdf.")

    criteria = criteria or ""
    if len(criteria) > MAX_CRITERIA_CHARS or "\x00" in criteria:
        raise InvalidUpload(f"Текст критериев должен занимать до {MAX_CRITERIA_CHARS} символов.")
    if criteria and criteria_pdf:
        raise InvalidUpload("Передайте критерии либо текстом, либо файлом PDF.")
    if criteria_pages and criteria_pdf is None:
        raise InvalidUpload("Диапазон страниц задаётся для PDF с критериями.")
    if not uploads or len(uploads) > MAX_PDFS:
        raise InvalidUpload(f"Загрузите от 1 до {MAX_PDFS} файлов PDF или ZIP.")
    return criteria, uploads, criteria_pdf, criteria_pages or None


def stage_pdfs(uploads, destination, *, file_issues=None):
    names = {}
    total_bytes = 0
    problems = file_issues if file_issues is not None else []
    statuses = []

    def reject(name, error, archive=None):
        label = re.sub(r"[\x00-\x1f\x7f]", "�", name)[:256] or "Файл без имени"
        issue = {"file": label, "reason": str(error)}
        if archive is not None:
            issue["archive"] = archive
        statuses.append(error.status)
        problems.append(issue)

    def save_pdf(name, stream, size):
        nonlocal total_bytes
        candidate = Path(name)
        if not valid_filename(name) or candidate.suffix.lower() != ".pdf" or not candidate.stem.strip(". "):
            raise InvalidUpload("Недопустимое имя PDF в загрузке.")
        if name.casefold() in names:
            raise InvalidUpload("PDF с таким именем уже добавлен.")
        if len(names) >= MAX_PDFS or size > MAX_PDF_BYTES or total_bytes + size > MAX_TOTAL_PDF_BYTES:
            raise InvalidUpload("Лимит — 100 PDF, 64 МиБ на PDF и 256 МиБ после распаковки.", status=413)
        prefix = stream.read(1024)
        if b"%PDF-" not in prefix:
            raise InvalidUpload("В файле отсутствует заголовок PDF.")
        path = destination / name
        target = path.open("xb")
        try:
            with target:
                target.write(prefix)
                shutil.copyfileobj(stream, target, MIB)
        except BaseException:
            path.unlink()
            raise
        names[name.casefold()] = name
        total_bytes += size

    archive_errors = (zipfile.BadZipFile, NotImplementedError, RuntimeError, EOFError, zlib.error, lzma.LZMAError)
    member_errors = (*archive_errors, InvalidUpload)

    def unpack(name, data):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if len(entries) > 5000:
                raise InvalidUpload("Слишком много элементов в ZIP.", status=413)
            candidates = 0
            archive_issues = 0
            for entry in entries:
                parts = entry.filename.replace("\\", "/").split("/")
                basename = parts[-1]
                if entry.is_dir() or not basename or "__MACOSX" in parts or basename.startswith("._"):
                    continue
                try:
                    if not basename.lower().endswith(".pdf"):
                        raise InvalidUpload("Формат внутри ZIP не поддерживается. Используйте PDF.", status=415)
                    candidates += 1
                    if (any(not valid_filename(part) for part in parts)
                            or stat.S_ISLNK(entry.external_attr >> 16)
                            or "\x00" in entry.orig_filename):
                        raise InvalidUpload("Недопустимый путь или ссылка на PDF внутри ZIP.")
                    with archive.open(entry) as stream:
                        save_pdf(basename, stream, entry.file_size)
                except member_errors as error:
                    archive_issues += 1
                    if archive_issues <= MAX_PDFS:
                        issue = (error if isinstance(error, InvalidUpload)
                                 else InvalidUpload("Не удалось распаковать этот PDF. Проверьте ZIP и пароль."))
                        reject(entry.orig_filename, issue, name)
            if archive_issues > MAX_PDFS:
                reject(name, InvalidUpload(f"Остальные пропущенные элементы ZIP — {archive_issues - MAX_PDFS}."))
            if not candidates:
                raise InvalidUpload("В ZIP нет файлов PDF.")

    for name, data in uploads:
        try:
            if not valid_filename(name):
                raise InvalidUpload("Недопустимое имя файла.")
            extension = Path(name).suffix.lower()
            if extension == ".pdf":
                save_pdf(name, io.BytesIO(data), len(data))
            elif extension == ".zip":
                try:
                    unpack(name, data)
                except archive_errors as exc:
                    raise InvalidUpload("Не удалось открыть ZIP. Проверьте, что архив цел и без пароля.") from exc
            else:
                raise InvalidUpload("Формат не поддерживается. Используйте PDF или ZIP.", status=415)
        except InvalidUpload as error:
            reject(name, error)
    if not names:
        status = statuses[0] if statuses and len(set(statuses)) == 1 else 400
        raise InvalidUpload("Не удалось принять ни одного PDF. Проверьте перечисленные файлы.",
                            status=status, file_issues=problems)
    return list(names.values())
