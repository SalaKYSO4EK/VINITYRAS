import type { FileIssue } from "@/lib/api";

export default function FileDiagnostics({ skipped = [], outcomes = [] }: { skipped?: FileIssue[]; outcomes?: FileIssue[] }) {
  return <>
    <FileList title="Файлы, пропущенные при загрузке" entries={skipped} />
    <FileList title="Результат по файлам" entries={outcomes} />
  </>;
}

function FileList({ title, entries }: { title: string; entries: FileIssue[] }) {
  if (!entries.length) return null;
  return <section className="mt-4 text-left text-sm">
    <h2 className="font-semibold">{title}</h2>
    <ul className="mt-2 space-y-2 list-disc pl-5" aria-label={title}>
      {entries.map(({ file, archive, reason }, i) => (
        <li key={i} className="break-words">
          <strong>{archive ? `${archive} / ${file}` : file}</strong>{" — "}{reason}
        </li>
      ))}
    </ul>
  </section>;
}
