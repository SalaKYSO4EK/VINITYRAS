import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, useSyncExternalStore, type ReactNode } from "react";
import { csvArtifacts, getHealth, getJob, getResult, JOB_ID, RequestError, statusLabel, submitJob, type CriteriaInput, type FileIssue, type Job, type JobProgress, type JobStatus, type Result } from "@/lib/api";
import { useJob } from "@/lib/use-job";

type Phase = JobStatus | "idle" | "uploading" | "checking";
type HistoryTab = "all" | "pending" | "completed";

interface HistoryEntry {
  id: string;
  files: string[];
  createdAt: string;
  status: JobStatus;
  tablesCount?: number;
  hasArchive?: boolean;
}

interface AppState {
  connection: "checking" | "ready" | "no-model" | "unavailable";
  phase: Phase;
  busy: boolean;
  names: string[];
  jobId: string | null;
  result?: Result;
  error?: string;
  uploadIssues?: FileIssue[];
  fileOutcomes?: FileIssue[];
  progress?: JobProgress;
  reused?: boolean;
  startProcessing: (files: File[], criteria: CriteriaInput) => Promise<void>;
  resetFlow: () => void;
  history: HistoryEntry[];
  clearHistory: () => void;
  accountOpen: boolean;
  openAccount: (tab?: HistoryTab) => void;
  closeAccount: () => void;
  accountTab: HistoryTab;
  setAccountTab: (tab: HistoryTab) => void;
  howOpen: boolean;
  setHowOpen: (value: boolean) => void;
  toast: (message: string) => void;
  toastMsg: string;
  toastVisible: boolean;
}

const AppContext = createContext<AppState | null>(null);
const STORAGE_KEY = "nexus_real_jobs_v1";
const ACTIVE_KEY = "nexus_current_job_v1";
const HISTORY_EVENT = "nexus-history-changed";

export function useApp() {
  const app = useContext(AppContext);
  if (!app) throw new Error("AppProvider is missing");
  return app;
}

function historySnapshot() {
  try { return localStorage.getItem(STORAGE_KEY) || "[]"; } catch { return "[]"; }
}

function parseHistory(snapshot: string): HistoryEntry[] {
  try {
    const jobs: HistoryEntry[] = JSON.parse(snapshot);
    if (Array.isArray(jobs)) {
      return jobs.filter((job) => job && JOB_ID.test(job.id) && Array.isArray(job.files)
        && typeof job.createdAt === "string" && job.files.every((name) => typeof name === "string")
        && Object.hasOwn(statusLabel, job.status));
    }
  } catch { /* Ignore outdated local history. */ }
  return [];
}

function subscribeHistory(update: () => void) {
  window.addEventListener(HISTORY_EVENT, update);
  window.addEventListener("storage", update);
  return () => {
    window.removeEventListener(HISTORY_EVENT, update);
    window.removeEventListener("storage", update);
  };
}

function saveHistory(jobs: HistoryEntry[]) {
  const snapshot = JSON.stringify(jobs);
  if (snapshot === historySnapshot()) return;
  try {
    localStorage.setItem(STORAGE_KEY, snapshot);
    window.dispatchEvent(new Event(HISTORY_EVENT));
  } catch { /* History remains optional when storage is disabled. */ }
}

function remember(job: Job, result?: Result, existingOnly = false) {
  const history = parseHistory(historySnapshot());
  const previous = history.find((entry) => entry.id === job.job_id);
  if (existingOnly && !previous) return;
  const entry: HistoryEntry = {
    id: job.job_id,
    files: job.source_files,
    createdAt: job.created_at,
    status: job.status,
    tablesCount: result ? csvArtifacts(result).length : previous?.tablesCount,
    hasArchive: result ? Boolean(result.download_url) : previous?.hasArchive,
  };
  saveHistory([entry, ...history.filter((item) => item.id !== entry.id)]
    .sort((a, b) => b.createdAt.localeCompare(a.createdAt)).slice(0, 100));
}

function saveActive(id: string | null) {
  try {
    if (id) sessionStorage.setItem(ACTIVE_KEY, id);
    else sessionStorage.removeItem(ACTIVE_KEY);
  } catch { /* Processing continues without session storage. */ }
}

export default function AppProvider({ children }: { children: ReactNode }) {
  const [connection, setConnection] = useState<AppState["connection"]>("checking");
  const [jobId, setJobId] = useState<string | null>(null);
  const [upload, setUpload] = useState<{ names: string[]; error?: string; issues?: FileIssue[] }>();
  const [accountOpen, setAccountOpen] = useState(false);
  const [accountTab, setAccountTab] = useState<HistoryTab>("all");
  const [howOpen, setHowOpen] = useState(false);
  const [toastMsg, setToastMsg] = useState("");
  const [toastVisible, setToastVisible] = useState(false);
  const uploading = useRef(false);
  const toastTimer = useRef<ReturnType<typeof setTimeout>>(null);

  const storedHistory = useSyncExternalStore(subscribeHistory, historySnapshot, () => "[]");
  const history = useMemo(() => parseHistory(storedHistory), [storedHistory]);
  const snapshot = useJob(jobId);
  const job = snapshot?.job;
  const result = snapshot?.result;
  const error = upload?.error || snapshot?.error || job?.error?.message;
  let phase: Phase = "idle";
  if (error || job?.status === "failed") phase = "failed";
  else if (upload) phase = "uploading";
  else if (job) phase = job.status;
  else if (jobId) phase = "checking";
  const busy = phase === "uploading" || phase === "checking" || phase === "queued" || phase === "running";
  const names = upload?.names ?? job?.source_files ?? history.find((entry) => entry.id === jobId)?.files ?? [];

  useEffect(() => {
    const controller = new AbortController();
    async function initialize() {
      try {
        const health = await getHealth(controller.signal);
        if (!controller.signal.aborted) setConnection(health.runner_configured ? "ready" : "no-model");
      } catch {
        if (!controller.signal.aborted) setConnection("unavailable");
      }
      if (controller.signal.aborted || uploading.current) return;
      try {
        const saved = sessionStorage.getItem(ACTIVE_KEY);
        if (saved && JOB_ID.test(saved)) setJobId((current) => current ?? saved);
      } catch { /* No saved job. */ }
    }
    void initialize();
    return () => controller.abort();
  }, []);

  useEffect(() => {
    if (job) remember(job, result, true);
  }, [job, result]);

  const otherPending = history.filter((entry) => entry.id !== jobId &&
    (entry.status === "running" || entry.status === "queued")).map((entry) => entry.id).join(",");
  useEffect(() => {
    if (!accountOpen || !otherPending) return;
    const controller = new AbortController();
    const { signal } = controller;
    let timer: ReturnType<typeof setTimeout>;
    async function refresh() {
      for (const id of otherPending.split(",")) {
        try {
          const updated = await getJob(id, signal);
          const output = updated.status === "completed" ? await getResult(id, signal) : undefined;
          if (!signal.aborted) remember(updated, output, true);
        } catch {
          // Keep the last known state while the server is unavailable.
        }
        if (signal.aborted) return;
      }
      timer = setTimeout(refresh, 5000);
    }
    void refresh();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [accountOpen, otherPending]);

  async function startProcessing(files: File[], criteria: CriteriaInput) {
    if (busy || uploading.current) return;
    const fileNames = files.map((file) => file.name);
    uploading.current = true;
    setJobId(null);
    saveActive(null);
    setUpload({ names: fileNames });
    try {
      const accepted = await submitJob(files, criteria);
      remember(accepted);
      saveActive(accepted.job_id);
      setJobId(accepted.job_id);
      setConnection("ready");
      setUpload(undefined);
    } catch (failure) {
      setUpload({
        names: fileNames,
        error: failure instanceof Error ? failure.message : "Не удалось отправить задание.",
        issues: failure instanceof RequestError ? failure.fileIssues : undefined,
      });
    } finally {
      uploading.current = false;
    }
  }

  function resetFlow() {
    if (uploading.current) return;
    setJobId(null);
    setUpload(undefined);
    saveActive(null);
  }

  const openAccount = useCallback((tab: HistoryTab = "all") => {
    setAccountTab(tab);
    setAccountOpen(true);
  }, []);
  const closeAccount = useCallback(() => setAccountOpen(false), []);

  const toast = useCallback((message: string) => {
    setToastMsg(message);
    setToastVisible(true);
    if (toastTimer.current) clearTimeout(toastTimer.current);
    toastTimer.current = setTimeout(() => setToastVisible(false), 3200);
  }, []);
  useEffect(() => () => { if (toastTimer.current) clearTimeout(toastTimer.current); }, []);

  function clearHistory() {
    saveHistory([]);
    toast("Список заданий в браузере очищен");
  }

  return (
    <AppContext.Provider value={{
      connection, phase, busy, names, jobId, result, error, progress: job?.progress, reused: job?.reused,
      uploadIssues: upload?.issues ?? job?.upload_issues, fileOutcomes: result?.file_outcomes ?? job?.file_outcomes,
      startProcessing, resetFlow, history, clearHistory,
      accountOpen, openAccount, closeAccount, accountTab, setAccountTab, howOpen, setHowOpen,
      toast, toastMsg, toastVisible,
    }}>
      {children}
    </AppContext.Provider>
  );
}
