import { useEffect, useState } from "react";
import { getJob, getResult, JOB_ID, type Job, type Result } from "./api";

interface Snapshot {
  id: string;
  job?: Job;
  result?: Result;
  error?: string;
}

export function useJob(id: string | null) {
  const [last, setLast] = useState<Snapshot | null>(null);

  useEffect(() => {
    if (!id || !JOB_ID.test(id)) return;
    const controller = new AbortController();
    const { signal } = controller;
    let timer: ReturnType<typeof setTimeout>;
    let retries = 0;

    async function poll(jobId: string) {
      try {
        const job = await getJob(jobId, signal);
        const result = job.status === "completed" ? await getResult(jobId, signal) : undefined;
        if (signal.aborted) return;
        retries = 0;
        setLast({ id: jobId, job, result });
        if (job.status === "queued" || job.status === "running") timer = setTimeout(() => poll(jobId), 1200);
      } catch (error) {
        if (signal.aborted) return;
        if (retries < 2) {
          retries++;
          timer = setTimeout(() => poll(jobId), 1500);
          return;
        }
        setLast({ id: jobId, error: error instanceof Error ? error.message : "Не удалось получить результат." });
      }
    }

    void poll(id);
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [id]);

  return last?.id === id ? last : null;
}
