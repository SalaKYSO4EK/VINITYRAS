import type { ReactNode } from "react";
import AppProvider from "@/components/AppProvider";
import SiteHeader from "@/components/SiteHeader";
import AccountDrawer from "@/components/AccountDrawer";
import HowModal from "@/components/HowModal";
import SiteFooter from "@/components/SiteFooter";
import { DeerSprite } from "@/components/icons";

export default function App({ children }: { children: ReactNode }) {
  return (
    <AppProvider>
      <DeerSprite />
      <SiteHeader />
      <AccountDrawer />
      <HowModal />
      {children}
      <SiteFooter />
    </AppProvider>
  );
}
