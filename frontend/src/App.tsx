import { Navigate, Route, Routes } from "react-router";

import { RedirectIfAuthenticated, RequireAuth } from "./auth/RequireAuth";
import { AppLayout } from "./components/AppLayout";
import { ActionItemsPage } from "./pages/ActionItemsPage";
import { CalendarPage } from "./pages/CalendarPage";
import { CallHistoryPage } from "./pages/CallHistoryPage";
import { CallDetailPage } from "./pages/CallDetailPage";
import { CallPrepPage } from "./pages/CallPrepPage";
import { ContactDetailPage } from "./pages/ContactDetailPage";
import { ContactFormPage } from "./pages/ContactFormPage";
import { ContactsPage } from "./pages/ContactsPage";
import { DashboardPage } from "./pages/DashboardPage";
import { KnowledgePage } from "./pages/KnowledgePage";
import { LiveCallPage } from "./pages/LiveCallPage";
import { LoginPage } from "./pages/LoginPage";
import { PlanCallPage } from "./pages/PlanCallPage";
import { RegisterPage } from "./pages/RegisterPage";
import { SettingsPage } from "./pages/SettingsPage";

export function App() {
  return (
    <Routes>
      <Route
        path="/login"
        element={
          <RedirectIfAuthenticated>
            <LoginPage />
          </RedirectIfAuthenticated>
        }
      />
      <Route
        path="/register"
        element={
          <RedirectIfAuthenticated>
            <RegisterPage />
          </RedirectIfAuthenticated>
        }
      />
      <Route
        element={
          <RequireAuth>
            <AppLayout />
          </RequireAuth>
        }
      >
        <Route index element={<DashboardPage />} />
        <Route path="contacts" element={<ContactsPage />} />
        <Route path="contacts/new" element={<ContactFormPage />} />
        <Route path="contacts/:id" element={<ContactDetailPage />} />
        <Route path="contacts/:id/edit" element={<ContactFormPage />} />
        <Route path="contacts/:id/prepare" element={<PlanCallPage />} />
        <Route path="calls" element={<CallHistoryPage />} />
        <Route path="calls/:id" element={<CallDetailPage />} />
        <Route path="calls/:id/prepare" element={<CallPrepPage />} />
        <Route path="calls/:id/live" element={<LiveCallPage />} />
        <Route path="knowledge" element={<KnowledgePage />} />
        <Route path="action-items" element={<ActionItemsPage />} />
        <Route path="calendar" element={<CalendarPage />} />
        <Route path="settings" element={<SettingsPage />} />
      </Route>
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
