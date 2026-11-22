import { ArrowLeftIcon, DownloadIcon } from "@/components/icons";
import ReportDashboard from "@/components/ReportDashboard";
import FileDiagnostics from "@/components/FileDiagnostics";
import { JOB_ID, progressDetails, reuseNotice, statusLabel } from "@/lib/api";
import { useJob } from "@/lib/use-job";
import { useApp } from "@/components/AppProvider";

export default function DashboardPage({ id }: { id: string }) {
  const { resetFlow } = useApp();
  const valid = JOB_ID.test(id);
  const snapshot = useJob(valid ? id : null);
  const job = snapshot?.job;
  const reuse = reuseNotice(job?.reused, job?.progress);
  const result = snapshot?.result;
  const error = !valid ? "Задание не найдено." : snapshot?.error ?? job?.error?.message;
  const progress = job?.status === "queued" || job?.status === "running" ? job.progress : undefined;
  const details = progressDetails(progress);

  return (
    <main className="flex-1 px-3 sm:px-6 pt-6 sm:pt-8 pb-16">
      <div className="max-w-6xl mx-auto">
        <a href="/" onClick={resetFlow} className="inline-flex items-center gap-1.5 text-sm font-semibold text-[var(--navy-600)] hover:text-[var(--navy-950)]">
          <ArrowLeftIcon size={16} /> {job?.status === "failed" ? "Повторить загрузку" : "К загрузке статей"}
        </a>
        <div className="flex flex-wrap justify-between items-end gap-3 mt-2 mb-5">
          <div className="min-w-0">
            <h1 className="text-2xl sm:text-3xl font-bold tracking-tight">Дашборд обработки</h1>
            {job && <p className="text-sm text-[var(--navy-500)] mt-1 break-all">{job.source_files.join(", ")}</p>}
            {job?.status === "completed" && reuse && <p className="text-sm text-[var(--navy-500)] mt-1">{reuse}</p>}
          </div>
          {result?.download_url && <a className="btn btn-sm" href={result.download_url}><DownloadIcon size={16} /> Скачать ZIP</a>}
        </div>

        {job && result ? <>
          <div className="mb-5"><FileDiagnostics skipped={job.upload_issues} outcomes={result.file_outcomes} /></div>
          <ReportDashboard job={job} result={result} />
        </> : (
          <div className="card p-6" role="status">
            <p className={error ? "text-[#8B342C]" : "text-[var(--navy-600)]"}>
              {error || progress?.label || (job ? statusLabel[job.status] : "Проверяем статус задания…")}
            </p>
            {!error && details && <p className="text-sm text-[var(--navy-500)] mt-2">{details}</p>}
            <FileDiagnostics skipped={job?.upload_issues} outcomes={job?.file_outcomes} />
          </div>
        )}
      </div>
    </main>
  );
}
