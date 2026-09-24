import { EMAIL_PATTERN } from "./errors";

export interface ContactFormValues {
  name: string;
  organization: string;
  phone: string;
  email: string;
  designation: string;
  status: string;
  source: string;
  tags: string;
  notes: string;
  owner_user_id: string;
}

export function validateContact(v: ContactFormValues): Partial<Record<keyof ContactFormValues, string>> {
  const errors: Partial<Record<keyof ContactFormValues, string>> = {};
  if (!v.name.trim()) errors.name = "Name is required.";
  if (v.email.trim() && !EMAIL_PATTERN.test(v.email.trim())) errors.email = "Enter a valid email.";
  const digits = v.phone.replace(/[\s().-]/g, "");
  if (v.phone.trim() && !/^\+?[0-9]{6,15}$/.test(digits))
    errors.phone = "Use 6–15 digits, optionally starting with +.";
  return errors;
}
