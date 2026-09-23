import { useState, type ChangeEvent, type FormEvent } from "react";
import { Link, useNavigate } from "react-router";

import { useAuth } from "../auth/context";
import { Alert, AuthCard, Button, TextField } from "../components/ui";
import { EMAIL_PATTERN, MIN_PASSWORD_LENGTH, errorMessage } from "../lib/errors";

type Field = "full_name" | "company_name" | "email" | "password";

export function RegisterPage() {
  const { register } = useAuth();
  const navigate = useNavigate();
  const [values, setValues] = useState<Record<Field, string>>({
    full_name: "",
    company_name: "",
    email: "",
    password: "",
  });
  const [fieldErrors, setFieldErrors] = useState<Partial<Record<Field, string>>>({});
  const [formError, setFormError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const update = (field: Field) => (e: ChangeEvent<HTMLInputElement>) =>
    setValues((v) => ({ ...v, [field]: e.target.value }));

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    const errors: Partial<Record<Field, string>> = {};
    if (!values.full_name.trim()) errors.full_name = "Enter your name.";
    if (!values.company_name.trim()) errors.company_name = "Enter your company name.";
    if (!EMAIL_PATTERN.test(values.email.trim())) errors.email = "Enter a valid email address.";
    if (values.password.length < MIN_PASSWORD_LENGTH)
      errors.password = `Use at least ${MIN_PASSWORD_LENGTH} characters.`;
    setFieldErrors(errors);
    setFormError(null);
    if (Object.keys(errors).length > 0) return;

    setSubmitting(true);
    try {
      await register({
        full_name: values.full_name.trim(),
        company_name: values.company_name.trim(),
        email: values.email.trim(),
        password: values.password,
      });
      navigate("/", { replace: true });
    } catch (error) {
      setFormError(errorMessage(error));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <AuthCard title="Create your account">
      <form className="space-y-4" onSubmit={onSubmit} noValidate>
        {formError ? <Alert>{formError}</Alert> : null}
        <TextField
          label="Your name"
          autoComplete="name"
          value={values.full_name}
          onChange={update("full_name")}
          error={fieldErrors.full_name}
        />
        <TextField
          label="Company name"
          autoComplete="organization"
          value={values.company_name}
          onChange={update("company_name")}
          error={fieldErrors.company_name}
        />
        <TextField
          label="Work email"
          type="email"
          autoComplete="email"
          value={values.email}
          onChange={update("email")}
          error={fieldErrors.email}
        />
        <TextField
          label="Password"
          type="password"
          autoComplete="new-password"
          value={values.password}
          onChange={update("password")}
          error={fieldErrors.password}
          hint={`At least ${MIN_PASSWORD_LENGTH} characters.`}
        />
        <Button type="submit" className="w-full" disabled={submitting}>
          {submitting ? "Creating account…" : "Create account"}
        </Button>
      </form>
      <p className="mt-4 text-center text-sm text-slate-600">
        Already have an account?{" "}
        <Link to="/login" className="font-medium text-slate-900 underline">
          Sign in
        </Link>
      </p>
    </AuthCard>
  );
}
