import { Navigate, Route, Routes } from "react-router";

import { RedirectIfAuthenticated, RequireAuth } from "./auth/RequireAuth";
import { AppLayout } from "./components/AppLayout";
import { ActionItemsPage } from "./pages/ActionItemsPage";
import { CallDetailPage } from "./pages/CallDetailPage";
import { ContactDetailPage } from "./pages/ContactDetailPage";
import { ContactFormPage } from "./pages/ContactFormPage";
import { ContactsPage } from "./pages/ContactsPage";
import { DashboardPage } from "./pages/DashboardPage";
import { LoginPage } from "./pages/LoginPage";
import { PlanCallPage } from "./pages/PlanCallPage";
import { RegisterPage } from "./pages/RegisterPage";

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
        <Route path="calls/:id" element={<CallDetailPage />} />
        <Route path="action-items" element={<ActionItemsPage />} />
      </Route>
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
