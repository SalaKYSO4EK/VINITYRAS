import { ArrowLeftIcon, DownloadIcon } from "@/components/icons";
import ReportDashboard from "@/components/ReportDashboard";
import { JOB_ID, statusLabel } from "@/lib/api";
import { useJob } from "@/lib/use-job";

export default function DashboardPage({ id }: { id: string }) {
  const valid = JOB_ID.test(id);
  const snapshot = useJob(valid ? id : null);
  const job = snapshot?.job;
  const result = snapshot?.result;
  const error = !valid ? "Задание не найдено." : snapshot?.error ?? job?.error?.message;

  return (
    <main className="flex-1 px-3 sm:px-6 pt-6 sm:pt-8 pb-16">
      <div className="max-w-6xl mx-auto">
        <a href="/" className="inline-flex items-center gap-1.5 text-sm font-semibold text-[var(--navy-600)] hover:text-[var(--navy-950)]">
          <ArrowLeftIcon size={16} /> К загрузке статей
        </a>
        <div className="flex flex-wrap justify-between items-end gap-3 mt-2 mb-5">
          <div className="min-w-0">
            <h1 className="text-2xl sm:text-3xl font-bold tracking-tight">Дашборд обработки</h1>
            {job && <p className="text-sm text-[var(--navy-500)] mt-1 break-all">{job.source_files.join(", ")}</p>}
          </div>
          {result?.download_url && <a className="btn btn-sm" href={result.download_url}><DownloadIcon size={16} /> Скачать ZIP</a>}
        </div>

        {job && result ? <ReportDashboard job={job} result={result} /> : (
          <div className="card p-6" role="status">
            <p className={error ? "text-[#8B342C]" : "text-[var(--navy-600)]"}>
              {error || (job ? statusLabel[job.status] : "Проверяем статус задания…")}
            </p>
          </div>
        )}
      </div>
    </main>
  );
}
