import { createRoot } from "react-dom/client";
import App from "./app/layout";
import HomePage from "./app/page";
import DashboardPage from "./app/dashboard/[id]/page";
import "./app/globals.css";

const path = window.location.pathname.split("/").filter(Boolean);
const dashboard = path[0] === "dashboard";

createRoot(document.getElementById("app")!).render(
  <App>{dashboard ? <DashboardPage id={path[1] ?? ""} /> : <HomePage />}</App>,
);
